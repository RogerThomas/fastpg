"""Slow queries: pg_sleep, lock waits, statement_timeout, and cancelling them with
``asyncio.timeout``.

There is no server-side cancel: a cancelled statement runs on to its end on the server, and
the statements behind it on its connection wait for it.
"""

import asyncio
import time
from typing import TYPE_CHECKING

import pytest
from faults import with_params

import fastpg
from fastpg import errors

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@pytest.fixture
async def locked_row(conn: fastpg.Connection) -> AsyncIterator[str]:
    await conn.execute_script(
        "DROP TABLE IF EXISTS net_slow_items;"
        " CREATE TABLE net_slow_items (id int PRIMARY KEY, v text);"
        " INSERT INTO net_slow_items VALUES (1, 'first')",
    )
    yield "net_slow_items"
    await conn.execute_script("DROP TABLE net_slow_items")


async def test_cancelled_sleep_keeps_its_connection_busy(conn: fastpg.Connection) -> None:
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.2):
            await conn.select_one("SELECT pg_sleep(1)")
    assert time.monotonic() - start < 0.5
    assert await conn.select_one("SELECT 1") == (1,)  # queued behind the sleep
    assert 0.8 < time.monotonic() - start < 2


@pytest.mark.usefixtures("locked_row")
async def test_cancelled_write_still_commits_once_its_lock_is_free(
    conn: fastpg.Connection,
    dsn: str,
) -> None:
    """Cancelling the caller doesn't cancel the statement: the UPDATE waiting for the lock
    still runs, and commits, when the lock is released."""
    async with await fastpg.connect(dsn) as writer, conn.transaction() as tx:
        await tx.select_one("SELECT v FROM net_slow_items WHERE id = 1 FOR UPDATE")
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.2):
                await writer.execute("UPDATE net_slow_items SET v = 'late' WHERE id = 1")
    # The transaction committed (releasing the lock), the writer's statement went on.
    async with asyncio.timeout(5):
        while await conn.select_one("SELECT v FROM net_slow_items WHERE id = 1") != ("late",):
            await asyncio.sleep(0.01)


async def test_statement_timeout_from_the_dsn(dsn: str) -> None:
    capped = with_params(dsn, options="-c%20statement_timeout%3D100")
    async with await fastpg.connect(capped) as conn:
        start = time.monotonic()
        with pytest.raises(errors.QueryCanceledError) as exc:
            await conn.select_one("SELECT pg_sleep(2)")
        assert exc.value.sqlstate == "57014"
        assert time.monotonic() - start < 1
        assert await conn.select_one("SELECT 1") == (1,)  # usable
    async with await fastpg.create_pool(capped, min_size=1, max_size=1) as pool:
        (pid,) = await pool.select_one("SELECT pg_backend_pid()")
        with pytest.raises(errors.QueryCanceledError):
            await pool.select_one("SELECT pg_sleep(2)")
        assert await pool.select_one("SELECT pg_backend_pid()") == (pid,)  # kept, not replaced


async def _fast_queries(pool: fastpg.Pool, count: int) -> float:
    start = time.monotonic()
    for i in range(count):
        assert await pool.select_one("SELECT $1::int", i) == (i,)
    return time.monotonic() - start


async def test_a_cancelled_slow_query_does_not_hold_up_the_pool(dsn: str) -> None:
    async with await fastpg.create_pool(dsn, min_size=2, max_size=2) as pool:
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.1):
                await pool.select_one("SELECT pg_sleep(1)")
        assert pool.stats().in_flight == 1  # still running on the server
        # Every fast query goes to the other connection meanwhile.
        assert await _fast_queries(pool, 20) < 0.5
        async with asyncio.timeout(3):
            while pool.stats().in_flight:
                await asyncio.sleep(0.01)
        assert pool.stats().idle == 2
