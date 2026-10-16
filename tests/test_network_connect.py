"""Connecting when the database is down, refusing, or unreachable: for a connection, when a
pool starts, and when it grows."""

import asyncio
import time
from typing import TYPE_CHECKING

import pytest
from faults import with_params

import fastpg

if TYPE_CHECKING:
    from faults import FaultProxy


@pytest.mark.parametrize(
    ("dsn", "message"),
    [
        (
            "postgresql://fastpg@127.0.0.1:1/fastpg?keepalives=1",
            'unsupported connection option "keepalives"',
        ),
        (
            "host=127.0.0.1 tcp_user_timeout=1000",
            'unsupported connection option "tcp_user_timeout"',
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
