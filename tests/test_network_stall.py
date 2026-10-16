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

    from faults import FaultProxy


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


async def test_close_completes_after_an_earlier_close_timed_out(
    proxied_dsn: str,
    fault_proxy: FaultProxy,
) -> None:
    conn = await fastpg.connect(proxied_dsn)
    fault_proxy.stall()
    # Close is graceful: it waits for the server to end the session.
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.2):
            await conn.close()
    fault_proxy.resume()
    async with asyncio.timeout(5):
        await conn.close()
    assert conn.closed


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
