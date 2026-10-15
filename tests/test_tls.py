"""TLS (``sslmode`` and friends) on both connection paths.

Tests against the TLS-only server use ``FASTPG_TLS_DSN`` (a URL without TLS keys whose host
is a name in the server certificate, e.g. ``localhost``) and ``FASTPG_TLS_CA`` (the server's
CA certificate; the client certificate and key are expected next to it). Without them, a
throwaway server is started, unless ``FASTPG_DSN`` is set: then they are skipped (see the
``tls_server`` fixture in conftest.py). The other tests only need the plain server.
"""

import asyncio
import socket
import ssl
import struct
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import quote, urlsplit, urlunsplit

import msgspec
import pytest

import fastpg
from fastpg import errors

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


class Item(msgspec.Struct):
    id: int
    name: str


def _with(dsn: str, **params: str | Path) -> str:
    """`dsn` with extra URL parameters."""
    query = "&".join(f"{key}={quote(str(value), safe='/')}" for key, value in params.items())
    return f"{dsn}{'&' if '?' in dsn else '?'}{query}"


def _at(dsn: str, *, host: str | None = None, port: int | None = None) -> str:
    """URL `dsn` pointed at another host and/or port."""
    parts = urlsplit(dsn)
    credentials = parts.netloc.rpartition("@")[0]
    netloc = f"{credentials}@{host or parts.hostname}:{port or parts.port}"
    return urlunsplit(parts._replace(netloc=netloc))


def _as(dsn: str, user: str) -> str:
    """URL `dsn` with another user and no password."""
    parts = urlsplit(dsn)
    return urlunsplit(parts._replace(netloc=f"{user}@{parts.netloc.rpartition('@')[2]}"))


async def _ssl_in_use(conn: fastpg.Connection) -> bool:
    sql = "SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()"
    (ssl,) = await conn.select_one(sql)
    return ssl


@dataclass(slots=True, frozen=True)
class TLSServer:
    """The TLS-only test server: every plaintext connection is rejected."""

    dsn: str
    ca: Path

    @property
    def verified_dsn(self) -> str:
        return _with(self.dsn, sslmode="verify-full", sslrootcert=self.ca)

    def by_address(self, dsn: str) -> str:
        """`dsn` with the server's IPv4 address as host: a name its certificate lacks."""
        host = urlsplit(self.dsn).hostname or "localhost"
        address = socket.getaddrinfo(host, None, socket.AF_INET)[0][4][0]
        return _at(dsn, host=str(address))


@dataclass(slots=True, eq=False)
class Proxy:
    """A TCP forwarder that can drop its connections abruptly (no TLS close_notify)."""

    _target: tuple[str, int]
    _writers: list[asyncio.StreamWriter] = field(default_factory=list[asyncio.StreamWriter])
    _server: asyncio.Server | None = None

    async def _pipe(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with suppress(OSError):
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        writer.close()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        upstream_reader, upstream_writer = await asyncio.open_connection(*self._target)
        self._writers += [writer, upstream_writer]
        await asyncio.gather(
            self._pipe(reader, upstream_writer),
            self._pipe(upstream_reader, writer),
        )

    async def start(self) -> int:
        """Listen on 127.0.0.1; return the port."""
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self._server.sockets[0].getsockname()[1]

    def cut(self) -> None:
        """Close every forwarded connection at once, as a network failure would."""
        for writer in self._writers:
            writer.transport.abort()

    async def close(self) -> None:
        self.cut()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()


def _auth_request(code: int, data: bytes = b"") -> bytes:
    """An Authentication message of type `code`."""
    return b"R" + struct.pack("!ii", 8 + len(data), code) + data


@dataclass(slots=True, eq=False)
class FakeServer:
    """Accepts TLS, answers the startup message with one authentication request, and records
    what the client sends after it, until the client closes the connection."""

    _context: ssl.SSLContext
    _request: bytes = b""
    _received: asyncio.Queue[bytes] = field(default_factory=asyncio.Queue[bytes])
    _server: asyncio.Server | None = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readexactly(8)  # SSLRequest
        writer.write(b"S")
        await writer.drain()
        await writer.start_tls(self._context)
        (length,) = struct.unpack("!i", await reader.readexactly(4))
        await reader.readexactly(length - 4)  # the rest of the startup message
        writer.write(self._request)
        await writer.drain()
        received = bytearray()
        with suppress(OSError):
            while data := await reader.read(65536):
                received += data
        self._received.put_nowait(bytes(received))
        writer.close()

    async def start(self, request: bytes) -> int:
        """Listen on 127.0.0.1, answering with `request`; return the port."""
        self._request = request
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self._server.sockets[0].getsockname()[1]

    async def received(self) -> bytes:
        """What the client sent after the authentication request."""
        async with asyncio.timeout(5):
            return await self._received.get()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()


@pytest.fixture
def server(tls_server: tuple[str, Path]) -> TLSServer:
    return TLSServer(*tls_server)


@pytest.fixture
async def tls_conn(server: TLSServer) -> AsyncIterator[fastpg.Connection]:
    async with await fastpg.connect(server.verified_dsn) as conn:
        yield conn


@pytest.fixture
async def proxy(server: TLSServer) -> AsyncIterator[Proxy]:
    target = urlsplit(server.dsn)
    forwarder = Proxy((target.hostname or "localhost", target.port or 5432))
    yield forwarder
    await forwarder.close()


@pytest.fixture
async def fake_server(server: TLSServer) -> AsyncIterator[FakeServer]:
    cert, key = server.ca.with_name("server.crt"), server.ca.with_name("server.key")
    if not (cert.exists() and key.exists()):
        pytest.skip("no server.crt/server.key next to FASTPG_TLS_CA")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    fake = FakeServer(context)
    yield fake
    await fake.close()


# --- against the TLS-only server --------------------------------------------------------


async def test_verify_full(tls_conn: fastpg.Connection) -> None:
    # SCRAM authentication over TLS: the server only accepts it channel-bound
    # (SCRAM-SHA-256-PLUS), here with the SHA-384 hash of the test certificate.
    assert await _ssl_in_use(tls_conn)
    assert await tls_conn.select_one("SELECT 1, 'a'") == (1, "a")


async def test_verify_full_rejects_another_host_name(server: TLSServer) -> None:
    with pytest.raises(fastpg.InterfaceError, match=r"not valid for name .*sslmode=verify-ca"):
        await fastpg.connect(server.by_address(server.verified_dsn))


async def test_verify_ca_skips_the_host_name(server: TLSServer) -> None:
    dsn = _with(server.by_address(server.dsn), sslmode="verify-ca", sslrootcert=server.ca)
    async with await fastpg.connect(dsn) as conn:
        assert await _ssl_in_use(conn)


@pytest.mark.parametrize(
    "params",
    [{"sslmode": "verify-full"}, {"sslrootcert": "system"}],
    ids=["no-sslrootcert", "sslrootcert-system"],
)
async def test_verify_full_uses_system_roots_without_sslrootcert(
    server: TLSServer,
    params: dict[str, str],
) -> None:
    # The test CA is not a system root.
    with pytest.raises(fastpg.InterfaceError, match=r"UnknownIssuer.*set sslrootcert"):
        await fastpg.connect(_with(server.dsn, **params))


@pytest.mark.parametrize("sslmode", ["require", "prefer"])
async def test_encrypts_without_verification(server: TLSServer, sslmode: str) -> None:
    async with await fastpg.connect(_with(server.dsn, sslmode=sslmode)) as conn:
        assert await _ssl_in_use(conn)


async def test_prefer_is_the_default(server: TLSServer) -> None:
    async with await fastpg.connect(server.dsn) as conn:
        assert await _ssl_in_use(conn)


async def test_require_with_sslrootcert_verifies_the_chain(server: TLSServer) -> None:
    # As in libpq: an existing sslrootcert file makes require check the chain (not the name),
    # a missing one is ignored.
    not_the_ca = server.ca.with_name("client.crt")
    if not not_the_ca.exists():
        pytest.skip("no client.crt next to FASTPG_TLS_CA")
    with pytest.raises(fastpg.InterfaceError, match="UnknownIssuer"):
        await fastpg.connect(_with(server.dsn, sslmode="require", sslrootcert=not_the_ca))
    missing = server.ca.with_name("missing.crt")
    async with await fastpg.connect(
        _with(server.dsn, sslmode="require", sslrootcert=missing),
    ) as conn:
        assert await _ssl_in_use(conn)


async def test_disable_sends_plaintext(server: TLSServer) -> None:
    # A plaintext connection matches no line of the server's pg_hba.conf.
    with pytest.raises(fastpg.DatabaseError, match="no encryption") as exc:
        await fastpg.connect(_with(server.dsn, sslmode="disable"))
    assert exc.value.sqlstate == "28000"


async def test_direct_negotiation(server: TLSServer) -> None:
    dsn = _with(server.verified_dsn, sslnegotiation="direct")
    async with await fastpg.connect(dsn) as conn:
        assert await _ssl_in_use(conn)


async def test_client_certificate(server: TLSServer) -> None:
    cert, key = server.ca.with_name("client.crt"), server.ca.with_name("client.key")
    if not (cert.exists() and key.exists()):
        pytest.skip("no client.crt/client.key next to FASTPG_TLS_CA")
    # The server authenticates fastpg_cert by its certificate only.
    dsn = _as(server.verified_dsn, "fastpg_cert")
    async with await fastpg.connect(_with(dsn, sslcert=cert, sslkey=key)) as conn:
        assert await conn.select_one("SELECT current_user") == ("fastpg_cert",)
    with pytest.raises(fastpg.DatabaseError) as exc:
        await fastpg.connect(dsn)
    assert exc.value.sqlstate == "28000"


async def test_queries_structs_and_writes(tls_conn: fastpg.Connection) -> None:
    rows = await tls_conn.select_many("SELECT i, repeat('x', 100) FROM generate_series(1, 20000) i")
    assert len(rows) == 20_000
    assert rows[-1] == (20_000, "x" * 100)
    items = await tls_conn.select_many(
        "SELECT i AS id, 'item-' || i AS name FROM generate_series(1, $1) i",
        3,
        into=Item,
    )
    assert items == [Item(1, "item-1"), Item(2, "item-2"), Item(3, "item-3")]
    results = await asyncio.gather(*(tls_conn.select_one("SELECT $1::int", i) for i in range(200)))
    assert results == [(i,) for i in range(200)]
    await tls_conn.execute_script(
        "DROP TABLE IF EXISTS tls_write_test; CREATE TABLE tls_write_test (id int, v text);",
    )
    sql = "INSERT INTO tls_write_test VALUES ($1, $2)"
    assert await tls_conn.execute_many(sql, [(1, "a"), (2, "b")]) == 2
    # Several MB: far past rustls' 64 KiB send buffer, so writes meet backpressure.
    copied = await tls_conn.copy_in("tls_write_test", [(i, "v" * 200) for i in range(3, 30_003)])
    assert copied == 30_000
    assert await tls_conn.select_one("SELECT count(*), max(id) FROM tls_write_test") == (
        30_002,
        30_002,
    )
    await tls_conn.execute_script("DROP TABLE tls_write_test")


async def test_server_terminating_connection_fails_promptly(
    tls_conn: fastpg.Connection,
    server: TLSServer,
) -> None:
    victim = await fastpg.connect(server.verified_dsn)
    (pid,) = await victim.select_one("SELECT pg_backend_pid()")
    sleeping = [asyncio.create_task(victim.select_one("SELECT pg_sleep(30)")) for _ in range(3)]
    await asyncio.sleep(0.1)
    await tls_conn.select_one("SELECT pg_terminate_backend($1)", pid)
    async with asyncio.timeout(5):  # must fail promptly, never hang
        for task in sleeping:
            with pytest.raises((errors.OperationalError, fastpg.ConnectionClosedError)):
                await task
        while not victim.closed:
            await asyncio.sleep(0.01)
    with pytest.raises(fastpg.ConnectionClosedError):
        await victim.select_one("SELECT 1")
    await victim.close()


async def test_connection_cut_without_close_notify_fails_promptly(
    server: TLSServer,
    proxy: Proxy,
) -> None:
    port = await proxy.start()
    dsn = _with(_at(server.dsn, host="127.0.0.1", port=port), sslmode="require")
    async with await fastpg.connect(dsn) as conn:
        assert await _ssl_in_use(conn)
        sleeping = asyncio.create_task(conn.select_one("SELECT pg_sleep(30)"))
        await asyncio.sleep(0.1)
        proxy.cut()
        async with asyncio.timeout(5):  # must fail promptly, never hang
            with pytest.raises(fastpg.ConnectionClosedError):
                await sleeping
            while not conn.closed:
                await asyncio.sleep(0.01)


@pytest.mark.parametrize("channel_binding", ["require", "disable"])
async def test_channel_binding(server: TLSServer, channel_binding: str) -> None:
    # The server offers SCRAM-SHA-256-PLUS: `require` uses it, `disable` plain SCRAM-SHA-256.
    for dsn in (server.verified_dsn, _with(server.dsn, sslmode="require")):
        async with await fastpg.connect(_with(dsn, channel_binding=channel_binding)) as conn:
            assert await _ssl_in_use(conn)
            assert await conn.select_one("SELECT 1") == (1,)


async def test_channel_binding_require_reports_login_errors(server: TLSServer) -> None:
    parts = urlsplit(_with(server.dsn, sslmode="require", channel_binding="require"))
    wrong_password = parts._replace(netloc=parts.netloc.replace(":fastpg@", ":wrong@"))
    with pytest.raises(fastpg.DatabaseError) as exc:
        await fastpg.connect(urlunsplit(wrong_password))
    assert exc.value.sqlstate == "28P01"
    with pytest.raises(fastpg.DatabaseError) as exc:
        await fastpg.connect(urlunsplit(parts._replace(path="/missing")))
    assert exc.value.sqlstate == "3D000"
    with pytest.raises(fastpg.InterfaceError, match="password is missing"):
        await fastpg.connect(_as(urlunsplit(parts), "fastpg"))


@pytest.mark.parametrize(
    ("auth_request", "message"),
    [
        (_auth_request(0), "server authenticated the client without channel binding"),
        (_auth_request(3), "server requested password authentication"),
        (_auth_request(5, b"salt"), "server requested md5 authentication"),
        (_auth_request(10, b"SCRAM-SHA-256\0\0"), "server did not offer SCRAM-SHA-256-PLUS"),
    ],
    ids=["trust", "password", "md5", "scram-without-plus"],
)
async def test_channel_binding_require_refuses_unbound_logins(
    server: TLSServer,
    fake_server: FakeServer,
    auth_request: bytes,
    message: str,
) -> None:
    port = await fake_server.start(auth_request)
    dsn = _at(server.dsn, host="127.0.0.1", port=port)
    with pytest.raises(fastpg.InterfaceError, match=f"channel binding required, but the {message}"):
        await fastpg.connect(_with(dsn, sslmode="require", channel_binding="require"))
    # Nothing was sent in answer: no password, hashed or not.
    assert await fake_server.received() == b""


# --- against the plain compose server ---------------------------------------------------


async def test_prefer_falls_back_to_plaintext(dsn: str) -> None:
    async with await fastpg.connect(_with(dsn, sslmode="prefer")) as conn:
        assert not await _ssl_in_use(conn)


async def test_disable(dsn: str) -> None:
    async with await fastpg.connect(_with(dsn, sslmode="disable")) as conn:
        assert not await _ssl_in_use(conn)


async def test_require_fails_without_server_support(dsn: str) -> None:
    with pytest.raises(fastpg.InterfaceError, match="server does not support SSL"):
        await fastpg.connect(_with(dsn, sslmode="require"))


async def test_channel_binding_require_fails_without_tls(dsn: str) -> None:
    # `prefer` falls back to plaintext against this server.
    with pytest.raises(fastpg.InterfaceError, match="needs TLS, but the connection is not"):
        await fastpg.connect(_with(dsn, channel_binding="require"))


async def test_channel_binding_disable_without_tls(dsn: str) -> None:
    async with await fastpg.connect(_with(dsn, channel_binding="disable")) as conn:
        assert not await _ssl_in_use(conn)


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"sslmode": "allow"}, "sslmode=allow is not supported"),
        ({"sslmode": "verify"}, "invalid sslmode"),
        ({"sslnegotiation": "direct"}, 'weak sslmode "prefer" may not be used'),
        ({"sslrootcert": "system", "sslmode": "require"}, "may not be used with sslrootcert"),
        ({"channel_binding": "bogus"}, 'invalid channel_binding value: "bogus"'),
        ({"sslmode": "disable", "channel_binding": "require"}, "channel_binding=require needs TLS"),
        ({"sslcert": "client.crt"}, "sslcert and sslkey must be given together"),
        ({"sslmode": "verify-ca", "sslrootcert": "missing.crt"}, "does not exist"),
    ],
)
async def test_invalid_settings_are_rejected(
    dsn: str,
    params: dict[str, str],
    message: str,
) -> None:
    with pytest.raises(fastpg.InterfaceError, match=message):
        await fastpg.connect(_with(dsn, **params))
