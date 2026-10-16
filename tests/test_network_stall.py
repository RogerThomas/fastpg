"""A server or network that stops responding (a `FaultProxy` stall, or a paused container).

What must hold: ``asyncio.timeout`` around any call returns on time; once the server answers
again, every connection and pool slot is usable or replaced; nothing is left open.
"""

import asyncio
import time
from typing import TYPE_CHECKING

import pytest

import fastpg

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Coroutine

    from faults import DisposableServer, FaultProxy


async def _settle(pool: fastpg.Pool) -> None:
    """Wait until nothing is in flight, waiting or opening."""
    async with asyncio.timeout(5):
        while (s := pool.stats()).in_flight or s.waiting or s.waiting_acquire or s.opening:
            await asyncio.sleep(0.01)


@pytest.fixture
async def stall_items(conn: fastpg.Connection) -> AsyncIterator[str]:
    await conn.execute_script(
        "DROP TABLE IF EXISTS net_stall_items; CREATE TABLE net_stall_items (id int)",
    )
    yield "net_stall_items"
    await conn.execute_script("DROP TABLE net_stall_items")


async def test_timed_out_query_leaves_the_connection_usable(
    proxied_dsn: str,
    fault_proxy: FaultProxy,
) -> None:
    async with await fastpg.connect(proxied_dsn) as conn:
        fault_proxy.stall()
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.2):
                await conn.select_one("SELECT 1")
        assert not conn.closed
        fault_proxy.resume()
        async with asyncio.timeout(5):
            # Its own result, not the abandoned query's (which arrives first).
            assert await conn.select_one("SELECT 2") == (2,)


async def _closed_by_client(proxy: FaultProxy, count: int) -> None:
    """Wait until fastpg has closed `count` sockets through `proxy`."""
    async with asyncio.timeout(2):
        while proxy.to_server.ended < count:
            await asyncio.sleep(0.01)


async def test_close_returns_promptly_from_a_stalled_server(
    proxied_dsn: str,
    fault_proxy: FaultProxy,
) -> None:
    conn = await fastpg.connect(proxied_dsn)
    fault_proxy.stall()
    # Terminate is written, then the socket is closed: no waiting for the server's EOF.
    async with asyncio.timeout(1):
        await conn.close()
    assert conn.closed
    await _closed_by_client(fault_proxy, 1)


async def test_async_with_exit_returns_promptly_from_a_stalled_server(
    proxied_dsn: str,
    fault_proxy: FaultProxy,
) -> None:
    async with asyncio.timeout(1):
        async with await fastpg.connect(proxied_dsn) as conn:
            await conn.select_one("SELECT 1")
            fault_proxy.stall()
    assert conn.closed
    await _closed_by_client(fault_proxy, 1)


async def test_tls_close_returns_promptly_from_a_stalled_server(
    tls_proxied_dsn: str,
    tls_fault_proxy: FaultProxy,
) -> None:
    conn = await fastpg.connect(tls_proxied_dsn)
    tls_fault_proxy.stall()
    async with asyncio.timeout(1):
        await conn.close()
    assert conn.closed
    await _closed_by_client(tls_fault_proxy, 1)


async def test_pool_close_returns_promptly_from_a_stalled_server(
    proxied_dsn: str,
    fault_proxy: FaultProxy,
) -> None:
    pool = await fastpg.create_pool(proxied_dsn, min_size=2, max_size=2)
    fault_proxy.stall()
    async with asyncio.timeout(1):
        await pool.close()
    assert pool.stats().size == 0
    await _closed_by_client(fault_proxy, 2)


async def test_a_timed_out_close_terminates_the_connection(
    proxied_dsn: str,
    fault_proxy: FaultProxy,
) -> None:
    conn = await fastpg.connect(proxied_dsn)
    fault_proxy.stall()
    stalled = asyncio.create_task(conn.select_one("SELECT 1"))
    await asyncio.sleep(0.05)
    # close() waits for the statement in flight; giving up on it closes at once.
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.2):
            await conn.close()
    assert conn.closed
    with pytest.raises(fastpg.ConnectionClosedError, match="connection is closed"):
        await stalled
    await _closed_by_client(fault_proxy, 1)
    async with asyncio.timeout(1):
        await conn.close()  # returns at once, rather than raising CancelledError


async def test_cancelled_pool_close_terminates_a_checked_out_connection(
    proxied_dsn: str,
    fault_proxy: FaultProxy,
) -> None:
    pool = await fastpg.create_pool(proxied_dsn, min_size=2, max_size=2)
    async with pool.acquire() as conn:
        fault_proxy.stall()
        stalled = asyncio.create_task(conn.select_one("SELECT 1"))
        await asyncio.sleep(0.05)
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.2):
                await pool.close()  # waits for the checked-out connection
        assert conn.closed
        with pytest.raises(fastpg.ConnectionClosedError):
            await stalled
        await _closed_by_client(fault_proxy, 2)  # the idle connection too
    async with asyncio.timeout(1):
        await pool.close()
    assert pool.stats().size == 0


async def _insert_then_stall(tx: fastpg.Transaction | fastpg.Connection, proxy: FaultProxy) -> None:
    await tx.execute("INSERT INTO net_stall_items VALUES (1)")
    proxy.stall()
    await tx.select_one("SELECT 1")  # unanswered


async def _in_connection_transaction(conn: fastpg.Connection, proxy: FaultProxy) -> None:
    async with conn.transaction() as tx:
        await _insert_then_stall(tx, proxy)


async def _in_pool_transaction(pool: fastpg.Pool, proxy: FaultProxy) -> None:
    async with pool.transaction() as tx:
        await _insert_then_stall(tx, proxy)


async def _in_acquired_transaction(pool: fastpg.Pool, proxy: FaultProxy) -> None:
    async with pool.acquire() as conn:
        await conn.execute_script("BEGIN")
        await _insert_then_stall(conn, proxy)


async def _times_out(work: Coroutine[object, object, None], seconds: float) -> float:
    """Await `work` under ``asyncio.timeout(seconds)``, which must fire; return the time taken."""
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(seconds):
            await work
    return time.monotonic() - start


@pytest.mark.usefixtures("stall_items")
async def test_timeout_bounds_a_connection_transaction(
    proxied_dsn: str,
    fault_proxy: FaultProxy,
) -> None:
    """The rollback on the way out must not wait for the stalled server."""
    async with await fastpg.connect(proxied_dsn) as conn:
        work = _in_connection_transaction(conn, fault_proxy)
        assert await _times_out(work, 0.3) < 1
        fault_proxy.resume()
        async with asyncio.timeout(5):
            # Rolled back (inside the transaction, its own insert would be visible).
            assert await conn.select_one("SELECT count(*) FROM net_stall_items") == (0,)


@pytest.mark.usefixtures("stall_items")
async def test_timeout_bounds_a_pool_transaction(
    proxied_dsn: str,
    fault_proxy: FaultProxy,
) -> None:
    async with await fastpg.create_pool(proxied_dsn, min_size=1, max_size=1) as pool:
        work = _in_pool_transaction(pool, fault_proxy)
        assert await _times_out(work, 0.3) < 1
        fault_proxy.resume()
        await _settle(pool)
        async with asyncio.timeout(5):
            assert await pool.select_one("SELECT count(*) FROM net_stall_items") == (0,)
        stats = pool.stats()
        assert (stats.size, stats.acquired, stats.in_flight) == (1, 0, 0)


@pytest.mark.usefixtures("stall_items")
async def test_timeout_bounds_an_acquired_connection(
    proxied_dsn: str,
    fault_proxy: FaultProxy,
) -> None:
    async with await fastpg.create_pool(proxied_dsn, min_size=1, max_size=1) as pool:
        work = _in_acquired_transaction(pool, fault_proxy)
        assert await _times_out(work, 0.3) < 1
        fault_proxy.resume()
        await _settle(pool)
        async with asyncio.timeout(5):
            assert await pool.select_one("SELECT count(*) FROM net_stall_items") == (0,)
        stats = pool.stats()
        assert (stats.size, stats.acquired, stats.in_flight) == (1, 0, 0)


async def test_cancelled_connect_to_a_stalled_server_closes_its_socket(
    proxied_dsn: str,
    fault_proxy: FaultProxy,
) -> None:
    fault_proxy.stall()  # the TCP connect succeeds, the startup is never answered
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.2):
            await fastpg.connect(proxied_dsn)
    async with asyncio.timeout(2):  # the socket is closed now, not once the server answers
        while fault_proxy.to_server.ended < 1:
            await asyncio.sleep(0.01)


async def test_pool_close_does_not_wait_for_a_stalled_connect(
    proxied_dsn: str,
    fault_proxy: FaultProxy,
) -> None:
    pool = await fastpg.create_pool(proxied_dsn, min_size=0, max_size=1)
    fault_proxy.stall()
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.2):
            await pool.select_one("SELECT 1")  # waits for the connection being opened
    assert pool.stats().opening == 1
    async with asyncio.timeout(2):
        await pool.close()
    stats = pool.stats()
    assert (stats.size, stats.opening) == (0, 0)


async def test_pool_recovers_once_the_server_answers_again(
    proxied_dsn: str,
    fault_proxy: FaultProxy,
) -> None:
    async with await fastpg.create_pool(proxied_dsn, min_size=1, max_size=2) as pool:
        fault_proxy.stall()
        for i in range(3):
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.2):
                    await pool.select_one("SELECT $1::int", i)
        # The abandoned statements keep their slots until the server is past them (there's
        # no server-side cancel), and the connection opened meanwhile waits for its startup.
        stats = pool.stats()
        assert (stats.in_flight, stats.opening) == (3, 1)
        fault_proxy.resume()
        await _settle(pool)
        async with asyncio.timeout(5):
            assert await pool.select_one("SELECT 4") == (4,)
        stats = pool.stats()
        assert (stats.size, stats.in_flight, stats.waiting) == (2, 0, 0)


async def test_stalled_tls_connection_recovers(
    tls_proxied_dsn: str,
    tls_fault_proxy: FaultProxy,
) -> None:
    async with await fastpg.connect(tls_proxied_dsn) as conn:
        tls_fault_proxy.stall()
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.2):
                await conn.select_one("SELECT 1")
        tls_fault_proxy.resume()
        async with asyncio.timeout(5):
            assert await conn.select_one("SELECT 2") == (2,)


# --- a paused server (docker pause: TCP stays up, nothing answers) --------------------------


@pytest.mark.timeout(60)
async def test_paused_server(edge: DisposableServer) -> None:
    async with (
        await fastpg.connect(edge.dsn) as conn,
        await fastpg.create_pool(edge.dsn, min_size=1, max_size=2) as pool,
    ):
        await edge.pause()
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.3):
                await conn.select_one("SELECT 1")
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.3):
                await pool.select_one("SELECT 1")
        # A new connection gets through TCP (Docker's port forwarder answers) but its
        # startup waits: only connect_timeout bounds it.
        start = time.monotonic()
        with pytest.raises(fastpg.InterfaceError, match="timeout expired"):
            await fastpg.connect(f"{edge.dsn}?connect_timeout=2")
        assert time.monotonic() - start < 3
        await edge.unpause()
        async with asyncio.timeout(10):
            assert await conn.select_one("SELECT 2") == (2,)
            assert await pool.select_one("SELECT 3") == (3,)
        await _settle(pool)
        stats = pool.stats()
        assert stats.size <= 2
        assert (stats.in_flight, stats.waiting) == (0, 0)
