"""Pool stress: thousands of tasks, dying connections, starvation, retirement under load."""

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

import fastpg
from fastpg import errors

if TYPE_CHECKING:
    from conftest import LoopErrors


@dataclass(slots=True)
class Load:
    """Counts what a stream of queries got back while it ran."""

    ok: int = 0
    failures: list[BaseException] = field(default_factory=list[BaseException])
    pids: set[int] = field(default_factory=set[int])


@pytest.fixture
def rng() -> random.Random:
    return random.Random(61016)  # noqa: S311 - reproducible timings, not cryptography


def _tagged(dsn: str, name: str) -> str:
    return f"{dsn}?application_name={name}"


async def _settled(pool: fastpg.Pool) -> fastpg.PoolStats:
    async with asyncio.timeout(10):
        # Checkouts too: one cancelled inside its block is given back in the background.
        while (
            (s := pool.stats()).in_flight
            or s.waiting
            or s.waiting_acquire
            or s.opening
            or s.acquired
        ):
            await asyncio.sleep(0.01)
    return pool.stats()


async def _backends(conn: fastpg.Connection, name: str) -> int:
    query = "SELECT count(*)::int FROM pg_stat_activity WHERE application_name = $1"
    (count,) = await conn.select_one(query, name)
    return count


async def _reacquire(pool: fastpg.Pool, stop: asyncio.Event) -> None:
    while not stop.is_set():
        async with pool.acquire() as conn:
            await conn.select_one("SELECT 1")


async def _share(pool: fastpg.Pool, stop: asyncio.Event) -> None:
    while not stop.is_set():
        await pool.select_one("SELECT 1")


async def _acquire_and_query(pool: fastpg.Pool) -> tuple[int]:
    async with pool.acquire() as conn:
        return await conn.select_one("SELECT 2")


async def _mixed(pool: fastpg.Pool, i: int) -> object:
    match i % 4:
        case 0:
            return await pool.select_one("SELECT $1::int", i)
        case 1:
            return await pool.select_one("SELECT $1::int / 0", i)
        case 2:
            return await pool.select_many("SELECT g FROM generate_series(1, 50) g")
        case _:
            return await pool.execute("SELECT $1::int", i)


def _check_mixed(task: asyncio.Task[object], i: int) -> None:
    if task.cancelled():
        return
    match i % 4:
        case 0:
            assert task.result() == (i,)
        case 1:
            assert isinstance(task.exception(), errors.DataError)
        case 2:
            assert task.result() == [(g,) for g in range(1, 51)]
        case _:
            assert task.result() == 1


async def _stream(pool: fastpg.Pool, stop: asyncio.Event, load: Load) -> None:
    """Shared queries until ``stop``, noting their backends and failures."""
    while not stop.is_set():
        try:
            (pid,) = await pool.select_one("SELECT pg_backend_pid()")
        except fastpg.Error as e:
            load.failures.append(e)
            await asyncio.sleep(0.001)
        else:
            load.ok += 1
            load.pids.add(pid)


async def _checkouts(pool: fastpg.Pool, stop: asyncio.Event, load: Load) -> None:
    """Transactions until ``stop``, noting their backends and failures."""
    while not stop.is_set():
        try:
            async with pool.transaction() as tx:
                (pid,) = await tx.select_one("SELECT pg_backend_pid()")
        except fastpg.Error as e:
            load.failures.append(e)
            await asyncio.sleep(0.001)
        else:
            load.ok += 1
            load.pids.add(pid)


async def _kill_backends(conn: fastpg.Connection, name: str, times: int) -> int:
    killed = 0
    for _ in range(times):
        await asyncio.sleep(0.1)
        (count,) = await conn.select_one(
            "SELECT count(pg_terminate_backend(pid))::int FROM pg_stat_activity"
            " WHERE application_name = $1",
            name,
        )
        killed += count
    return killed


async def test_shared_statements_are_not_starved_by_repeated_acquires(dsn: str) -> None:
    async with await fastpg.create_pool(dsn, min_size=1, max_size=1) as pool:
        stop = asyncio.Event()
        hogs = [asyncio.create_task(_reacquire(pool, stop)) for _ in range(4)]
        await asyncio.sleep(0.05)  # every release now finds acquire() waiters queued
        try:
            async with asyncio.timeout(3):
                assert await pool.select_one("SELECT 2") == (2,)
        finally:
            stop.set()
            await asyncio.gather(*hogs)


async def test_acquire_is_not_starved_by_a_flood_of_shared_statements(dsn: str) -> None:
    async with await fastpg.create_pool(dsn, min_size=2, max_size=2) as pool:
        stop = asyncio.Event()
        flood = [asyncio.create_task(_share(pool, stop)) for _ in range(100)]
        await asyncio.sleep(0.05)
        try:
            async with asyncio.timeout(3):
                assert await _acquire_and_query(pool) == (2,)
        finally:
            stop.set()
            await asyncio.gather(*flood)


@pytest.mark.timeout(120)
async def test_ten_thousand_tasks_with_cancellations_and_errors(
    dsn: str,
    rng: random.Random,
    loop_errors: LoopErrors,
) -> None:
    async with await fastpg.create_pool(dsn, min_size=1, max_size=8) as pool:
        tasks = [asyncio.create_task(_mixed(pool, i)) for i in range(10_000)]
        for task in rng.sample(tasks, 2_500):
            if rng.random() < 0.1:
                await asyncio.sleep(0)
            task.cancel()
        await asyncio.wait(tasks)
        for i, task in enumerate(tasks):
            _check_mixed(task, i)
        stats = await _settled(pool)
        assert (stats.in_flight, stats.waiting, stats.acquired) == (0, 0, 0)
        assert stats.size <= 8
        assert stats.idle == stats.size
        assert await asyncio.gather(
            *(pool.select_one("SELECT $1::int", i) for i in range(100)),
        ) == [(i,) for i in range(100)]
    assert loop_errors.messages() == []


@pytest.mark.timeout(60)
async def test_connections_dying_at_max_size_under_load(
    dsn: str,
    conn: fastpg.Connection,
    loop_errors: LoopErrors,
) -> None:
    name = "edge_pool_dying"
    async with await fastpg.create_pool(_tagged(dsn, name), min_size=3, max_size=3) as pool:
        stop = asyncio.Event()
        load = Load()
        workers = [asyncio.create_task(_stream(pool, stop, load)) for _ in range(30)]
        workers += [asyncio.create_task(_checkouts(pool, stop, load)) for _ in range(3)]
        killed = await _kill_backends(conn, name, 5)
        await asyncio.sleep(0.2)
        stop.set()
        async with asyncio.timeout(10):  # nothing hangs on a dead connection
            await asyncio.gather(*workers)
        assert killed >= 5
        # Only the statements on a killed session fail, with a connection-level error.
        for failure in load.failures:
            assert isinstance(failure, fastpg.ConnectionClosedError | errors.OperationalError)
        assert load.ok > 100
        stats = await _settled(pool)
        assert stats.size <= 3
        assert (stats.acquired, stats.in_flight) == (0, 0)
        assert await pool.select_one("SELECT 1") == (1,)
        assert await _backends(conn, name) == pool.stats().size
    assert loop_errors.messages() == []


@pytest.mark.timeout(60)
async def test_retirement_while_busy(dsn: str, conn: fastpg.Connection) -> None:
    name = "edge_pool_retire"
    pool = await fastpg.create_pool(
        _tagged(dsn, name),
        min_size=1,
        max_size=4,
        max_lifetime=0.2,
        max_idle=0.1,
    )
    async with pool:
        stop = asyncio.Event()
        load = Load()
        workers = [asyncio.create_task(_stream(pool, stop, load)) for _ in range(20)]
        workers += [asyncio.create_task(_checkouts(pool, stop, load)) for _ in range(2)]
        await asyncio.sleep(1.5)
        stop.set()
        await asyncio.gather(*workers)
        assert load.failures == []  # retiring never fails a statement
        assert len(load.pids) > 4  # connections were replaced as they aged
        # Idle again: back down to min_size, with no session left behind.
        deadline = time.monotonic() + 5
        while pool.stats().size > 1 and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert pool.stats().size == 1
        async with asyncio.timeout(5):
            while await _backends(conn, name) != pool.stats().size:
                await asyncio.sleep(0.05)


@pytest.mark.timeout(30)
async def test_retirement_replaces_before_closing(dsn: str) -> None:
    # An idle pool at min_size opens an aged connection's replacement before closing it, so
    # it never has fewer than min_size open (a statement would wait for a connect meanwhile).
    pool = await fastpg.create_pool(dsn, min_size=1, max_size=4, max_lifetime=0.1, max_idle=0.05)
    async with pool:
        pids: set[int] = set()
        sizes: list[int] = []
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            for _ in range(20):
                sizes.append(pool.stats().size)
                await asyncio.sleep(0.002)
            (pid,) = await pool.select_one("SELECT pg_backend_pid()")
            pids.add(pid)
        assert len(pids) > 3  # connections were replaced as they aged
        assert min(sizes) >= 1


@pytest.mark.timeout(30)
async def test_retirement_at_max_size_closes_first(dsn: str) -> None:
    # With min_size == max_size there's no room to open the replacement first: an aged
    # connection is closed, then replaced, and the pool keeps working within max_size.
    pool = await fastpg.create_pool(dsn, min_size=2, max_size=2, max_lifetime=0.1)
    async with pool:
        pids: set[int] = set()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            (pid,) = await pool.select_one("SELECT pg_backend_pid()")
            pids.add(pid)
            stats = pool.stats()
            assert stats.size + stats.opening <= 2
            await asyncio.sleep(0.02)
        assert len(pids) > 2
