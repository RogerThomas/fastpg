"""Settings the DSN leaves out come from libpq's PG* environment variables, and the password
from the password file (``~/.pgpass``, ``PGPASSFILE`` or ``passfile=``), as in libpq. With no
host, the local unix socket; with no user, the OS user's login name."""

import os
import pwd
import re
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest
from faults import FaultProxy, with_params

import fastpg


@dataclass(frozen=True, slots=True)
class Server:
    """The test server's settings, taken from its DSN."""

    host: str
    port: int
    user: str
    password: str
    dbname: str

    def dsn(self, **params: str) -> str:
        """A URL DSN for it, without the password."""
        dsn = f"postgresql://{self.user}@{self.host}:{self.port}/{self.dbname}"
        return with_params(dsn, **params) if params else dsn


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No PG* variable from the environment the tests run in, and a home directory without a
    password file."""
    for name in [name for name in os.environ if name.startswith("PG")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("HOME", str(tmp_path))


@pytest.fixture
def server(dsn: str) -> Server:
    url = urlsplit(dsn)
    return Server(
        host=url.hostname or "localhost",
        port=url.port or 5432,
        user=unquote(url.username or ""),
        password=unquote(url.password or ""),
        dbname=url.path.lstrip("/"),
    )


def password_file(path: Path, *lines: str, mode: int = 0o600) -> Path:
    path.write_text("".join(f"{line}\n" for line in lines))
    path.chmod(mode)
    return path


def entry(server: Server, password: str | None = None) -> str:
    """The password file line for `server`."""
    password = server.password if password is None else password
    return f"{server.host}:{server.port}:{server.dbname}:{server.user}:{password}"


# --- environment variables ------------------------------------------------------------------


async def test_an_empty_dsn_takes_everything_from_the_environment(
    server: Server,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PGHOST", server.host)
    monkeypatch.setenv("PGPORT", str(server.port))
    monkeypatch.setenv("PGUSER", server.user)
    monkeypatch.setenv("PGPASSWORD", server.password)
    monkeypatch.setenv("PGDATABASE", server.dbname)
    monkeypatch.setenv("PGAPPNAME", "from-env")
    monkeypatch.setenv("PGOPTIONS", "-c work_mem=7MB")
    async with await fastpg.connect("") as conn:
        row = await conn.select_one(
            "SELECT current_user, current_database(), current_setting('application_name'), "
            "current_setting('work_mem')",
        )
    assert row == (server.user, server.dbname, "from-env", "7MB")


async def test_a_partial_dsn_takes_the_rest_from_the_environment(
    server: Server,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PGHOST", server.host)
    monkeypatch.setenv("PGPORT", str(server.port))
    monkeypatch.setenv("PGPASSWORD", server.password)
    async with await fastpg.connect(f"user={server.user} dbname={server.dbname}") as conn:
        assert await conn.select_one("SELECT current_user") == (server.user,)


async def test_the_dsn_wins_over_the_environment(
    dsn: str,
    server: Server,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PGHOST", "/nonexistent-fastpg")
    monkeypatch.setenv("PGPORT", "1")
    monkeypatch.setenv("PGUSER", "nobody")
    monkeypatch.setenv("PGPASSWORD", "wrong")
    monkeypatch.setenv("PGDATABASE", "nonexistent")
    monkeypatch.setenv("PGAPPNAME", "from-env")
    async with await fastpg.connect(with_params(dsn, application_name="from-dsn")) as conn:
        row = await conn.select_one("SELECT current_user, current_setting('application_name')")
    assert row == (server.user, "from-dsn")


@pytest.mark.parametrize(
    ("variables", "message"),
    [
        ({"PGPORT": "x"}, 'invalid PGPORT value "x"'),
        ({"PGSSLMODE": "bogus"}, 'invalid sslmode value: "bogus"'),
        ({"PGSSLNEGOTIATION": "x"}, 'invalid sslnegotiation value: "x"'),
        ({"PGSSLSNI": "x"}, 'invalid sslsni value: "x"'),
        ({"PGCHANNELBINDING": "x"}, 'invalid channel_binding value: "x"'),
        ({"PGCONNECT_TIMEOUT": "x"}, 'invalid integer value "x" .* "connect_timeout"'),
        ({"PGTARGETSESSIONATTRS": "x"}, 'invalid target_session_attrs value: "x"'),
        ({"PGSSLCERT": "/client.crt"}, "sslcert and sslkey must be given together"),
        ({"PGSSLKEY": "/client.key"}, "sslcert and sslkey must be given together"),
        (
            {"PGSSLROOTCERT": "system", "PGSSLMODE": "require"},
            'weak sslmode "require" may not be used with sslrootcert=system',
        ),
    ],
)
async def test_each_variable_is_read(
    server: Server,
    monkeypatch: pytest.MonkeyPatch,
    variables: dict[str, str],
    message: str,
) -> None:
    """An invalid value in each variable is reported, as in the DSN: it was read."""
    for name, value in variables.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(fastpg.InterfaceError, match=message):
        await fastpg.connect(f"host={server.host}")


async def test_target_session_attrs_from_the_environment(
    server: Server,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PGPASSWORD", server.password)
    monkeypatch.setenv("PGTARGETSESSIONATTRS", "read-only")
    with pytest.raises(fastpg.ConnectError, match="session is not read-only") as exc:
        await fastpg.connect(server.dsn())
    assert isinstance(exc.value, fastpg.OperationalError)


async def test_create_pool_reads_the_environment(
    server: Server,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PGPASSWORD", server.password)
    monkeypatch.setenv("PGAPPNAME", "pool-from-env")
    async with await fastpg.create_pool(server.dsn(), min_size=1, max_size=1) as pool:
        row = await pool.select_one("SELECT current_setting('application_name')")
    assert row == ("pool-from-env",)


# --- password file --------------------------------------------------------------------------


async def test_the_password_comes_from_pgpass_in_the_home_directory(
    server: Server,
    tmp_path: Path,
) -> None:
    password_file(tmp_path / ".pgpass", entry(server))
    async with await fastpg.connect(server.dsn()) as conn:
        assert await conn.select_one("SELECT current_user") == (server.user,)


async def test_a_password_file_others_can_read_is_ignored_with_a_warning(
    server: Server,
    tmp_path: Path,
) -> None:
    password_file(tmp_path / ".pgpass", entry(server), mode=0o644)
    with (
        pytest.warns(UserWarning, match="has group or world access; permissions should be"),
        pytest.raises(fastpg.FastPGError, match="password is missing"),
    ):
        await fastpg.connect(server.dsn())


async def test_pgpassword_wins_over_the_password_file(
    server: Server,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    password_file(tmp_path / ".pgpass", entry(server, "wrong"))
    monkeypatch.setenv("PGPASSWORD", server.password)
    async with await fastpg.connect(server.dsn()) as conn:
        assert await conn.select_one("SELECT 1") == (1,)


async def test_wildcards_comments_and_the_first_match(server: Server, tmp_path: Path) -> None:
    password_file(
        tmp_path / ".pgpass",
        "# a comment",
        "otherhost:*:*:*:wrong",
        f"*:{server.port}:*:{server.user}:{server.password}",
        "*:*:*:*:wrong",
    )
    async with await fastpg.connect(server.dsn()) as conn:
        assert await conn.select_one("SELECT 1") == (1,)


@pytest.fixture
async def odd_role(dsn: str) -> AsyncIterator[tuple[str, str]]:
    """A role whose name and password hold a `:` and a `\\`: ``(name, password)``."""
    token = secrets.token_hex(4)
    name, password = f"fastpg:pgpass\\{token}", f"pass:word\\{token}"
    async with await fastpg.connect(dsn) as conn:
        await conn.execute(f"CREATE ROLE \"{name}\" LOGIN PASSWORD '{password}'")
    yield name, password
    async with await fastpg.connect(dsn) as conn:
        await conn.execute(f'DROP ROLE "{name}"')


async def test_backslash_escapes(
    server: Server,
    tmp_path: Path,
    odd_role: tuple[str, str],
) -> None:
    name, password = odd_role

    def escaped(value: str) -> str:
        return value.replace("\\", "\\\\").replace(":", "\\:")

    password_file(
        tmp_path / ".pgpass",
        f"*:*:*:{escaped(name)}:{escaped(password)}",
    )
    user = name.replace("\\", "\\\\")
    dsn = f"host={server.host} port={server.port} dbname={server.dbname} user='{user}'"
    async with await fastpg.connect(dsn) as conn:
        assert await conn.select_one("SELECT current_user") == (name,)


async def test_passfile_key_and_pgpassfile(
    server: Server,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    right = password_file(tmp_path / "right", entry(server))
    wrong = password_file(tmp_path / "wrong", entry(server, "wrong"))
    password_file(tmp_path / ".pgpass", entry(server, "wrong"))
    # PGPASSFILE wins over ~/.pgpass.
    monkeypatch.setenv("PGPASSFILE", str(right))
    async with await fastpg.connect(server.dsn()) as conn:
        assert await conn.select_one("SELECT 1") == (1,)
    # passfile= wins over PGPASSFILE.
    monkeypatch.setenv("PGPASSFILE", str(wrong))
    async with await fastpg.connect(server.dsn(passfile=str(right))) as conn:
        assert await conn.select_one("SELECT 1") == (1,)


# --- no host, no user -----------------------------------------------------------------------

# libpq's default socket directories, as fastpg tries them when nothing gives a host.
SOCKET_DIRS = ("/var/run/postgresql", "/run/postgresql", "/tmp")


def free_socket_port() -> int:
    """A port number with no server socket in any default socket directory."""
    while True:
        port = 40000 + secrets.randbelow(20000)
        if not any(Path(d, f".s.PGSQL.{port}").exists() for d in SOCKET_DIRS):
            return port


@pytest.fixture
async def local_socket(server: Server) -> AsyncIterator[tuple[int, FaultProxy]]:
    """``(port, proxy)``: a socket in `/tmp`, a default socket directory, for that port,
    forwarding to the test server."""
    port = free_socket_port()
    proxy = FaultProxy((server.host, server.port))
    await proxy.start_unix(f"/tmp/.s.PGSQL.{port}")
    yield port, proxy
    await proxy.close()
    Path(f"/tmp/.s.PGSQL.{port}").unlink(missing_ok=True)


async def test_with_no_host_the_local_socket_is_used(
    server: Server,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    local_socket: tuple[int, FaultProxy],
) -> None:
    """The password file's `localhost` entry is the one for the default socket, as in libpq."""
    port, proxy = local_socket
    password_file(
        tmp_path / ".pgpass",
        f"{server.host}:{port}:*:*:wrong",
        f"localhost:{port}:{server.dbname}:{server.user}:{server.password}",
    )
    monkeypatch.setenv("PGPORT", str(port))
    monkeypatch.setenv("PGUSER", server.user)
    monkeypatch.setenv("PGDATABASE", server.dbname)
    async with await fastpg.connect("") as conn:
        assert await conn.select_one("SELECT current_user") == (server.user,)
    assert proxy.accepted == 1


async def test_with_no_host_and_no_local_socket_the_error_says_where_it_looked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = free_socket_port()
    monkeypatch.setenv("PGUSER", "nobody")
    tried = ", ".join(f"{d}/.s.PGSQL.{port}" for d in SOCKET_DIRS)
    with pytest.raises(fastpg.ConnectError) as exc:
        await fastpg.connect(f"port={port}")
    assert str(exc.value) == (
        "could not connect to server: no host given, and no local server socket found "
        f"(tried {tried}); set host= or PGHOST"
    )


async def test_with_no_user_the_os_login_name_is_sent(
    server: Server,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The server is asked for the OS user's role (``getpwuid(geteuid())``, not ``$USER``)."""
    login = pwd.getpwuid(os.geteuid()).pw_name
    monkeypatch.setenv("USER", "not-the-login-name")
    monkeypatch.setenv("PGPASSWORD", f"wrong-{secrets.token_hex(4)}")
    with pytest.raises(fastpg.FastPGError, match=re.escape(f'"{login}"')):
        await fastpg.connect(f"host={server.host} port={server.port} dbname={server.dbname}")
