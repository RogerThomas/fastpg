"""Connecting when the database is down, refusing, or unreachable: for a connection, when a
pool starts, and when it grows."""

import asyncio
import time

import pytest
from faults import FaultProxy, with_params

import fastpg

# Every test here times something: run after the rest, alone (`task test`).
pytestmark = pytest.mark.timing


@pytest.mark.parametrize(
    ("dsn", "message"),
    [
        (
            "postgresql://fastpg@127.0.0.1:1/fastpg?gssencmode=disable",
            'unsupported connection option "gssencmode"',
        ),
        (
            "host=127.0.0.1 tcp_user_timeout=x",
            'invalid integer value "x" for connection option "tcp_user_timeout"',
        ),
        ("host=127.0.0.1 port=x", "invalid connection string"),
        (
            "host=127.0.0.1 connect_timeout=x",
            'invalid integer value "x" for connection option "connect_timeout"',
        ),
    ],
)
async def test_invalid_connection_options_are_named(dsn: str, message: str) -> None:
    with pytest.raises(fastpg.InterfaceError, match=message):
        await fastpg.connect(dsn)


# --- connect_timeout ------------------------------------------------------------------------


async def test_connect_timeout_bounds_a_startup_never_answered(
    proxied_dsn: str,
    fault_proxy: FaultProxy,
) -> None:
    """The TCP connect succeeds, the startup is never answered (an overloaded or paused
    server); libpq's minimum, 2 s, applies."""
    fault_proxy.stall()
    start = time.monotonic()
    with pytest.raises(fastpg.InterfaceError, match=r"startup .* timeout expired"):
        await fastpg.connect(with_params(proxied_dsn, connect_timeout="1"))
    assert 1.9 < time.monotonic() - start < 3
    async with asyncio.timeout(2):  # and its socket is closed
        while fault_proxy.to_server.ended < 1:
            await asyncio.sleep(0.01)


async def test_connect_timeout_bounds_an_address_that_never_answers() -> None:
    """192.0.2.1 (TEST-NET-1) is never routed: its SYNs go unanswered, and without a
    timeout the OS gives up only after a minute or more. Where it isn't routable at all,
    connecting fails at once instead."""
    start = time.monotonic()
    with pytest.raises(fastpg.InterfaceError, match="could not connect"):
        await fastpg.connect("postgresql://fastpg@192.0.2.1:5432/fastpg?connect_timeout=2")
    assert time.monotonic() - start < 3


async def test_connect_timeout_lets_a_slow_but_answering_server_in(
    proxied_dsn: str,
    fault_proxy: FaultProxy,
) -> None:
    """Each round trip of the startup takes 0.3 s: well within the timeout."""
    fault_proxy.to_client.delay = 0.3
    dsn = with_params(proxied_dsn, connect_timeout="3")
    async with await fastpg.connect(dsn) as conn:
        assert await conn.select_one("SELECT 1") == (1,)


@pytest.mark.parametrize("value", ["0", "-1"])
async def test_connect_timeout_zero_waits_indefinitely(
    proxied_dsn: str,
    fault_proxy: FaultProxy,
    value: str,
) -> None:
    fault_proxy.stall()
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(2.5):  # past libpq's 2 s minimum
            await fastpg.connect(with_params(proxied_dsn, connect_timeout=value))


async def test_create_pool_with_a_connect_timeout(
    proxied_dsn: str,
    fault_proxy: FaultProxy,
) -> None:
    fault_proxy.stall()
    start = time.monotonic()
    dsn = with_params(proxied_dsn, connect_timeout="2")
    with pytest.raises(fastpg.InterfaceError, match="timeout expired"):
        await fastpg.create_pool(dsn, min_size=3)
    assert time.monotonic() - start < 3
    async with asyncio.timeout(2):  # every socket closed
        while fault_proxy.to_server.ended < 3:
            await asyncio.sleep(0.01)


async def test_pool_growth_with_a_connect_timeout_fails_waiters(
    proxied_dsn: str,
    fault_proxy: FaultProxy,
) -> None:
    """With nothing open, a statement waits for the connection being opened; its timeout
    fails the statement instead of leaving it waiting forever."""
    dsn = with_params(proxied_dsn, connect_timeout="2")
    async with await fastpg.create_pool(dsn, min_size=0, max_size=2) as pool:
        fault_proxy.stall()
        start = time.monotonic()
        with pytest.raises(fastpg.InterfaceError, match="timeout expired"):
            await pool.select_one("SELECT 1")
        assert time.monotonic() - start < 3
        fault_proxy.resume()
        async with asyncio.timeout(5):
            assert await pool.select_one("SELECT 2") == (2,)


# --- without connect_timeout, and a database that refuses -----------------------------------


async def test_without_connect_timeout_create_pool_waits_for_the_startup(
    proxied_dsn: str,
    fault_proxy: FaultProxy,
) -> None:
    """Only the caller's own timeout ends it; the sockets are closed then."""
    fault_proxy.stall()
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(1):
            await fastpg.create_pool(proxied_dsn, min_size=2)
    async with asyncio.timeout(2):
        while fault_proxy.to_server.ended < 2:
            await asyncio.sleep(0.01)


async def test_a_refusing_database_fails_statements_at_once(
    proxied_dsn: str,
    fault_proxy: FaultProxy,
) -> None:
    """With nothing open, each statement starts a connect of its own and fails with it:
    there is no backoff, so the attempts come as fast as the statements."""
    async with await fastpg.create_pool(proxied_dsn, min_size=0, max_size=4) as pool:
        fault_proxy.refuse = True  # every new connection is reset at once
        start = time.monotonic()
        for _ in range(20):
            with pytest.raises(fastpg.InterfaceError):
                await pool.select_one("SELECT 1")
        assert time.monotonic() - start < 2
        assert fault_proxy.accepted == 20  # one connect attempt per statement
        fault_proxy.refuse = False
        assert await pool.select_one("SELECT 1") == (1,)


@pytest.mark.parametrize("max_size", [20, 4])
async def test_concurrent_statements_on_a_refusing_database_connect_once_each(
    proxied_dsn: str,
    fault_proxy: FaultProxy,
    max_size: int,
) -> None:
    """At most one connect attempt per waiting statement, and never more than max_size."""
    async with await fastpg.create_pool(proxied_dsn, min_size=0, max_size=max_size) as pool:
        fault_proxy.refuse = True
        queries = [pool.select_one("SELECT 1") for _ in range(10)]
        results = await asyncio.gather(*queries, return_exceptions=True)
        assert all(isinstance(r, fastpg.InterfaceError) for r in results)
        assert 1 <= fault_proxy.accepted <= min(10, max_size)


async def test_acquire_on_a_refusing_database_connects_once(
    proxied_dsn: str,
    fault_proxy: FaultProxy,
) -> None:
    async with await fastpg.create_pool(proxied_dsn, min_size=0, max_size=4) as pool:
        fault_proxy.refuse = True
        for _ in range(5):
            with pytest.raises(fastpg.InterfaceError):
                async with pool.acquire():
                    pass
        assert fault_proxy.accepted == 5


async def test_failing_growth_leaves_the_open_connections_serving(
    proxied_dsn: str,
    fault_proxy: FaultProxy,
) -> None:
    async with await fastpg.create_pool(proxied_dsn, min_size=1, max_size=4) as pool:
        fault_proxy.refuse = True
        queries = [pool.select_one("SELECT $1::int FROM pg_sleep(0.01)", i) for i in range(50)]
        results = await asyncio.gather(*queries)
        assert results == [(i,) for i in range(50)]  # pipelined on the one connection
        assert pool.stats().size == 1
        assert fault_proxy.accepted > 1  # it tried to grow, and gave up quietly
