"""libpq's TCP keepalive and ``tcp_user_timeout`` DSN keys.

Their effect (a dead peer noticed) needs probes or segments that go unacknowledged, which
no forwarder in user space can arrange (its kernel acknowledges them): these tests check
that every connection takes the keys, and how they're read.
"""

from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
from faults import with_params

import fastpg


@pytest.mark.parametrize(
    "params",
    [
        {
            "keepalives": "1",
            "keepalives_idle": "1",
            "keepalives_interval": "1",
            "keepalives_count": "1",
            "tcp_user_timeout": "1000",
        },
        {"keepalives": "0", "keepalives_idle": "1", "tcp_user_timeout": "1000"},
        {
            "keepalives_idle": "0",
            "keepalives_interval": "0",
            "keepalives_count": "0",
            "tcp_user_timeout": "0",
        },
        {"keepalives": "-1", "keepalives_idle": "-1", "tcp_user_timeout": "-1"},
        {"keepalives_idle": "%2030%20", "keepalives_count": "5"},
    ],
)
async def test_keepalive_keys_are_accepted(dsn: str, params: dict[str, str]) -> None:
    async with await fastpg.connect(with_params(dsn, **params)) as conn:
        assert await conn.select_one("SELECT 1") == (1,)


async def test_key_value_dsn_takes_the_keys(dsn: str) -> None:
    parts = urlsplit(dsn)
    key_value = (
        f"host={parts.hostname} port={parts.port} user={parts.username} "
        f"password={parts.password} dbname={parts.path.lstrip('/')} "
        "keepalives=1 keepalives_idle=1 keepalives_interval=1 keepalives_count=1 "
        "tcp_user_timeout=1000"
    )
    async with await fastpg.connect(key_value) as conn:
        assert await conn.select_one("SELECT 1") == (1,)


async def test_pool_connections_take_the_keys(dsn: str) -> None:
    keyed = with_params(dsn, keepalives_idle="1", keepalives_count="1", tcp_user_timeout="1000")
    async with await fastpg.create_pool(keyed, min_size=2, max_size=4) as pool:
        assert await pool.select_one("SELECT 1") == (1,)
        async with pool.acquire() as first, pool.acquire() as second:
            assert await first.select_one("SELECT 1") == (1,)
            assert await second.select_one("SELECT 1") == (1,)


async def test_multi_host_dsn_takes_the_keys(dsn: str) -> None:
    """Each address tried gets the keys: the refused first one, and the one that answers."""
    parts = urlsplit(dsn)
    credentials, _, address = parts.netloc.rpartition("@")
    multi = urlunsplit(parts._replace(netloc=f"{credentials}@127.0.0.1:1,{address}"))
    async with await fastpg.connect(with_params(multi, keepalives_idle="1")) as conn:
        assert await conn.select_one("SELECT 1") == (1,)


async def test_tls_connections_take_the_keys(tls_server: tuple[str, Path]) -> None:
    keyed = with_params(
        tls_server[0],
        sslmode="require",
        keepalives_idle="1",
        keepalives_interval="1",
        keepalives_count="1",
        tcp_user_timeout="1000",
    )
    async with await fastpg.connect(keyed) as conn:
        assert await conn.select_one("SELECT 1") == (1,)


async def test_unix_socket_ignores_the_keys(unix_dsn: str) -> None:
    keyed = with_params(
        unix_dsn,
        keepalives="1",
        keepalives_idle="1",
        keepalives_interval="1",
        keepalives_count="1",
        tcp_user_timeout="1000",
    )
    async with await fastpg.connect(keyed) as conn:
        assert await conn.select_one("SELECT 1") == (1,)


@pytest.mark.parametrize(
    "key",
    [
        "keepalives",
        "keepalives_idle",
        "keepalives_interval",
        "keepalives_count",
        "tcp_user_timeout",
    ],
)
@pytest.mark.parametrize("value", ["x", "1.5", "2147483648"])
async def test_invalid_values_raise(dsn: str, key: str, value: str) -> None:
    message = f'invalid integer value "{value}" for connection option "{key}"'
    with pytest.raises(fastpg.InterfaceError, match=message):
        await fastpg.connect(with_params(dsn, **{key: value}))


async def test_invalid_values_raise_for_a_pool(dsn: str) -> None:
    with pytest.raises(fastpg.InterfaceError, match="keepalives_idle"):
        await fastpg.create_pool(with_params(dsn, keepalives_idle="x"))


async def test_invalid_values_raise_for_a_unix_socket(unix_dsn: str) -> None:
    with pytest.raises(fastpg.InterfaceError, match="keepalives_count"):
        await fastpg.connect(with_params(unix_dsn, keepalives_count="x"))


async def test_a_value_the_os_rejects_fails_the_connect(dsn: str) -> None:
    """As in libpq, a setting the OS refuses fails the attempt, naming the option."""
    with pytest.raises(fastpg.InterfaceError, match=r"setsockopt\(TCP_KEEP\w+\) failed"):
        await fastpg.connect(with_params(dsn, keepalives_idle="2147483647"))
