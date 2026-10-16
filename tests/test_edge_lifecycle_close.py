"""Closing while busy, closing twice, and using a connection or pool after close."""

import asyncio
from typing import TYPE_CHECKING

import pytest

import fastpg

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from conftest import LoopErrors


class BlockError(Exception):
    """Raised inside an ``async with`` block."""


@pytest.fixture
async def table(conn: fastpg.Connection) -> AsyncIterator[None]:
    """A fresh ``edge_close (id int, v text)`` table."""
    await conn.execute_script(
        "DROP TABLE IF EXISTS edge_close; CREATE TABLE edge_close (id int, v text)",
    )
    yield
    await conn.execute_script("DROP TABLE edge_close")


def _rows(count: int) -> Iterator[tuple[int, str]]:
    return ((i, "v") for i in range(count))


async def _wait_for_backends(conn: fastpg.Connection, name: str, count: int) -> None:
    query = "SELECT count(*)::int FROM pg_stat_activity WHERE application_name = $1"
    async with asyncio.timeout(5):
        while (await conn.select_one(query, name))[0] != count:
            await asyncio.sleep(0.01)


async def _insert_after(conn: fastpg.Connection, gate: asyncio.Future[None]) -> None:
    async with conn.transaction() as tx:
        await tx.execute("INSERT INTO edge_close VALUES (1, 'a')")
        await gate
        await tx.execute("INSERT INTO edge_close VALUES (2, 'b')")


async def _pool_transaction_after(pool: fastpg.Pool, gate: asyncio.Future[None]) -> int:
    async with pool.transaction() as tx:
        await tx.execute("INSERT INTO edge_close VALUES (1, 'a')")
        await gate
        return await tx.execute("INSERT INTO edge_close VALUES (2, 'b')")


async def _hold(pool: fastpg.Pool, gate: asyncio.Future[None]) -> int:
    async with pool.acquire() as conn:
        await gate
        (value,) = await conn.select_one("SELECT 7")
        return value


async def _acquire_one(pool: fastpg.Pool) -> None:
    async with pool.acquire():
        pass


async def _raise_inside(resource: fastpg.Connection | fastpg.Pool) -> None:
    async with resource:
        raise BlockError


# --- connections ----------------------------------------------------------------------------


async def test_close_lets_pipelined_queries_and_a_streamed_result_finish(
    dsn: str,
    loop_errors: LoopErrors,
) -> None:
    conn = await fastpg.connect(dsn)
    many = asyncio.create_task(conn.select_many("SELECT g FROM generate_series(1, 50000) g"))
    ones = [asyncio.create_task(conn.select_one("SELECT $1::int", i)) for i in range(20)]
    await asyncio.sleep(0)  # all submitted
    await conn.close()
    assert len(await many) == 50000
    assert [await task for task in ones] == [(i,) for i in range(20)]
    assert loop_errors.messages() == []


@pytest.mark.usefixtures("table")
@pytest.mark.parametrize("method", ["copy_in", "execute_many", "insert_many"])
async def test_close_during_a_bulk_write_lets_it_commit(
    dsn: str,
    conn: fastpg.Connection,
    method: str,
) -> None:
    victim = await fastpg.connect(dsn)
    if method == "copy_in":
        write = victim.copy_in("edge_close", _rows(200_000))
    elif method == "execute_many":
        write = victim.execute_many("INSERT INTO edge_close VALUES ($1, $2)", _rows(20_000))
    else:
        write = victim.insert_many("edge_close", _rows(50_000))
    task = asyncio.create_task(write)
    await asyncio.sleep(0.01)
    queued = asyncio.create_task(victim.select_one("SELECT 42"))  # waits behind the write
    await asyncio.sleep(0)
    async with asyncio.timeout(20):
        await victim.close()
    written = await task
    assert await queued == (42,)
    assert await conn.select_one("SELECT count(*)::int FROM edge_close") == (written,)


@pytest.mark.usefixtures("table")
async def test_close_during_a_transaction_rolls_it_back(
    dsn: str,
    conn: fastpg.Connection,
) -> None:
    victim = await fastpg.connect(dsn)
    gate = asyncio.get_running_loop().create_future()
    block = asyncio.create_task(_insert_after(victim, gate))
    await asyncio.sleep(0.05)  # inside the block, first row inserted
    await victim.close()
    gate.set_result(None)
    with pytest.raises(fastpg.ConnectionClosedError) as exc:
        await block
    assert exc.value.__notes__ == ["fastpg: 'ROLLBACK' failed too: connection is closed"]
    assert await conn.select_one("SELECT count(*)::int FROM edge_close") == (0,)


async def test_close_twice_and_concurrently(dsn: str, loop_errors: LoopErrors) -> None:
    conn = await fastpg.connect(dsn)
    await asyncio.gather(conn.close(), conn.close(), conn.close())
    await conn.close()
    assert conn.closed
    assert loop_errors.messages() == []


async def test_every_method_fails_clearly_after_close(dsn: str) -> None:
    conn = await fastpg.connect(dsn)
    await conn.close()
    calls = [
        conn.select_one("SELECT 1"),
        conn.select_one_or_none("SELECT 1"),
        conn.select_many("SELECT 1"),
        conn.execute("SELECT 1"),
        conn.execute_script("SELECT 1"),
        conn.execute_many("SELECT $1::int", [(1,)]),
        conn.insert_many("t", [(1,)]),
        conn.copy_in("t", [(1,)]),
    ]
    for call in calls:
        with pytest.raises(fastpg.ConnectionClosedError, match="connection is closed"):
            await call
    with pytest.raises(fastpg.ConnectionClosedError, match="connection is closed"):
        async with conn.transaction():
            pass


async def test_async_with_closes_when_the_block_raises(dsn: str) -> None:
    conn = await fastpg.connect(dsn)
    with pytest.raises(BlockError):
        await _raise_inside(conn)
    assert conn.closed


async def test_pool_close_while_its_connections_are_opening(
    dsn: str,
    conn: fastpg.Connection,
) -> None:
    # The waiting statements fail, and the connections being opened for them are closed once
    # they are up, not left behind.
    tagged = f"{dsn}?application_name=edge_close_opening"
    pool = await fastpg.create_pool(tagged, min_size=0, max_size=4)
    queries = [asyncio.create_task(pool.select_one("SELECT pg_sleep(0.05)")) for _ in range(4)]
    await asyncio.sleep(0)  # connections are opening
    await pool.close()
    for task in queries:
        with pytest.raises(fastpg.ConnectionClosedError, match="pool is closed"):
            await task
    await _wait_for_backends(conn, "edge_close_opening", 0)


# --- pools ----------------------------------------------------------------------------------


async def test_pool_close_lets_shared_statements_finish_and_fails_waiters(
    dsn: str,
    loop_errors: LoopErrors,
) -> None:
    pool = await fastpg.create_pool(dsn, min_size=1, max_size=1, max_inflight_per_conn=2)
    running = [
        asyncio.create_task(pool.select_one("SELECT $1::int FROM pg_sleep(0.05)", i))
        for i in range(2)
    ]
    waiting = [asyncio.create_task(pool.select_one("SELECT 1")) for _ in range(3)]
    acquiring = asyncio.create_task(_acquire_one(pool))
    await asyncio.sleep(0.01)
    assert pool.stats().waiting == 3
    await pool.close()
    assert [await task for task in running] == [(0,), (1,)]
    for task in [*waiting, acquiring]:
        with pytest.raises(fastpg.ConnectionClosedError, match="pool is closed"):
            await task
    stats = pool.stats()
    assert (stats.size, stats.waiting, stats.waiting_acquire, stats.in_flight) == (0, 0, 0, 0)
    assert loop_errors.messages() == []


async def test_pool_close_waits_for_a_checked_out_connection(dsn: str) -> None:
    pool = await fastpg.create_pool(dsn, min_size=1, max_size=1)
    gate = asyncio.get_running_loop().create_future()
    holder = asyncio.create_task(_hold(pool, gate))
    await asyncio.sleep(0.01)
    closing = asyncio.create_task(pool.close())
    await asyncio.sleep(0.05)
    assert not closing.done()
    assert pool.closed
    gate.set_result(None)
    assert await holder == 7  # the checked-out connection still works until released
    async with asyncio.timeout(5):
        await closing
    assert pool.stats().size == 0


@pytest.mark.usefixtures("table")
async def test_pool_close_lets_an_open_transaction_commit(
    dsn: str,
    conn: fastpg.Connection,
) -> None:
    pool = await fastpg.create_pool(dsn, min_size=1, max_size=1)
    gate = asyncio.get_running_loop().create_future()
    block = asyncio.create_task(_pool_transaction_after(pool, gate))
    await asyncio.sleep(0.05)
    closing = asyncio.create_task(pool.close())
    await asyncio.sleep(0.01)
    gate.set_result(None)
    assert await block == 1
    async with asyncio.timeout(5):
        await closing
    assert await conn.select_one("SELECT count(*)::int FROM edge_close") == (2,)


async def test_pool_close_during_a_bulk_write(dsn: str, conn: fastpg.Connection) -> None:
    await conn.execute_script(
        "DROP TABLE IF EXISTS edge_close_bulk; CREATE TABLE edge_close_bulk (id int, v text)",
    )
    pool = await fastpg.create_pool(dsn, min_size=1, max_size=1)
    task = asyncio.create_task(pool.copy_in("edge_close_bulk", _rows(100_000)))
    await asyncio.sleep(0.01)
    async with asyncio.timeout(20):
        await pool.close()
    assert await task == 100_000
    assert await conn.select_one("SELECT count(*)::int FROM edge_close_bulk") == (100_000,)
    await conn.execute_script("DROP TABLE edge_close_bulk")


async def test_pool_close_twice_concurrently_and_cancelled(dsn: str) -> None:
    pool = await fastpg.create_pool(dsn, min_size=2, max_size=2)
    first = asyncio.create_task(pool.close())
    await asyncio.sleep(0)
    first.cancel()  # the shutdown itself goes on (shielded)
    with pytest.raises(asyncio.CancelledError):
        await first
    async with asyncio.timeout(5):
        await asyncio.gather(pool.close(), pool.close())
    assert pool.closed
    assert pool.stats().size == 0


async def test_every_pool_method_fails_clearly_after_close(dsn: str) -> None:
    pool = await fastpg.create_pool(dsn, min_size=1, max_size=1)
    await pool.close()
    calls = [
        pool.select_one("SELECT 1"),
        pool.select_one_or_none("SELECT 1"),
        pool.select_many("SELECT 1"),
        pool.execute("SELECT 1"),
        pool.execute_script("SELECT 1"),
        pool.execute_many("SELECT $1::int", [(1,)]),
        pool.insert_many("t", [(1,)]),
        pool.copy_in("t", [(1,)]),
        _acquire_one(pool),
    ]
    for call in calls:
        with pytest.raises(fastpg.ConnectionClosedError, match="pool is closed"):
            await call
    with pytest.raises(fastpg.ConnectionClosedError, match="pool is closed"):
        async with pool.transaction():
            pass


async def test_pool_async_with_closes_when_the_block_raises(dsn: str) -> None:
    pool = await fastpg.create_pool(dsn, min_size=1, max_size=1)
    with pytest.raises(BlockError):
        await _raise_inside(pool)
    assert pool.closed
    assert pool.stats().size == 0
