"""Pool behaviour: routing, caps, growth, exclusive checkouts, transactions, lifecycle."""

import asyncio
import datetime as dt
import time
from typing import TYPE_CHECKING, Annotated, Any

import msgspec
import pytest

import fastpg
from fastpg import errors

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable


class Item(msgspec.Struct):
    id: int
    v: str


class Positive(msgspec.Struct):
    """Needs validation (a constrained field), so ``strategy="direct"`` is unavailable."""

    id: Annotated[int, msgspec.Meta(gt=0)]


@pytest.fixture
async def items(conn: fastpg.Connection) -> AsyncIterator[None]:
    """A fresh ``pool_items (id int PRIMARY KEY, v text)`` table."""
    await conn.execute_script(
        "DROP TABLE IF EXISTS pool_items; CREATE TABLE pool_items (id int PRIMARY KEY, v text)",
    )
    yield
    await conn.execute_script("DROP TABLE pool_items")


@pytest.fixture
async def sequence(conn: fastpg.Connection) -> AsyncIterator[None]:
    """A fresh ``pool_seq`` sequence."""
    await conn.execute_script("DROP SEQUENCE IF EXISTS pool_seq; CREATE SEQUENCE pool_seq")
    yield
    await conn.execute_script("DROP SEQUENCE pool_seq")


async def _settle(pool: fastpg.Pool) -> None:
    """Wait until nothing is in flight, waiting or opening."""
    async with asyncio.timeout(5):
        while (s := pool.stats()).in_flight or s.waiting or s.waiting_acquire or s.opening:
            await asyncio.sleep(0.01)


async def _pid_of_sleeper(pool: fastpg.Pool, seconds: float) -> int:
    (pid, _) = await pool.select_one("SELECT pg_backend_pid(), pg_sleep($1)", seconds)
    return pid


# --- shared mode ----------------------------------------------------------------------------


async def test_shared_queries(dsn: str) -> None:
    async with await fastpg.create_pool(dsn) as pool:
        assert await pool.select_one("SELECT 1, 'a'") == (1, "a")
        assert await pool.select_one_or_none("SELECT 1 WHERE false") is None
        assert await pool.select_many("SELECT generate_series(1, 3)") == [(1,), (2,), (3,)]
        assert await pool.execute("SELECT generate_series(1, 4)") == 4
        await pool.execute_script("SELECT 1; SELECT 2")
        person = await pool.select_one(
            "SELECT 7 AS id, 'x' AS v",
            into=Item,
        )
        assert person == Item(id=7, v="x")
        assert await pool.select_one_or_none("SELECT 8 AS id, 'y' AS v", into=Item) == Item(8, "y")
        assert await pool.select_many("SELECT 9 AS id, 'z' AS v", into=Item) == [Item(9, "z")]
        with pytest.raises(fastpg.NoRowsError):
            await pool.select_one("SELECT 1 WHERE false")
        with pytest.raises(errors.SQLSyntaxError):
            await pool.select_one("SELEC 1")
        assert await pool.select_one("SELECT 2") == (2,)


@pytest.mark.parametrize("strategy", ["direct", "msgpack", "json"])
async def test_shared_queries_use_the_strategy(dsn: str, strategy: fastpg.Strategy) -> None:
    async with await fastpg.create_pool(dsn) as pool:
        sql = "SELECT 7 AS id, 'x' AS v"
        assert await pool.select_one(sql, into=Item, strategy=strategy) == Item(7, "x")
        assert await pool.select_one_or_none(sql, into=Item, strategy=strategy) == Item(7, "x")
        assert await pool.select_many(sql, into=Item, strategy=strategy) == [Item(7, "x")]
        assert await pool.select_one_or_none(f"{sql} WHERE false", into=Item) is None


async def test_shared_query_strategy_errors_reach_the_caller(dsn: str) -> None:
    async with await fastpg.create_pool(dsn) as pool:
        with pytest.raises(fastpg.UnsupportedTypeError):
            await pool.select_one("SELECT 1 AS id", into=Positive, strategy="direct")
        with pytest.raises(fastpg.DecodeError):
            await pool.select_one("SELECT 0 AS id", into=Positive)
        assert await pool.select_one("SELECT 2 AS id", into=Positive) == Positive(2)
        assert pool.stats().in_flight == 0


async def test_many_concurrent_shared_queries(dsn: str) -> None:
    async with await fastpg.create_pool(dsn, max_size=4, max_inflight_per_conn=4) as pool:
        ids = list(range(1, 301))
        results = await asyncio.gather(
            *(pool.select_one("SELECT id, name FROM people WHERE id = $1", i) for i in ids),
        )
        assert results == [(i, f"person-{i}") for i in ids]
        await _settle(pool)
        stats = pool.stats()
        assert stats.size <= 4
        assert stats.idle == stats.size


async def test_routes_to_an_idle_connection(dsn: str) -> None:
    async with await fastpg.create_pool(dsn, min_size=2, max_size=2) as pool:
        slow = asyncio.create_task(_pid_of_sleeper(pool, 0.3))
        await asyncio.sleep(0.05)
        # Not pipelined behind the sleeper: the idle connection answers right away.
        async with asyncio.timeout(0.2):
            (fast_pid,) = await pool.select_one("SELECT pg_backend_pid()")
        assert fast_pid != await slow


async def test_reuses_the_most_recently_idle_connection(dsn: str) -> None:
    async with await fastpg.create_pool(dsn, min_size=2, max_size=2) as pool:
        first_pid, last_pid = await asyncio.gather(
            _pid_of_sleeper(pool, 0.05),
            _pid_of_sleeper(pool, 0.15),
        )
        assert first_pid != last_pid
        # The connection that went idle last serves the next statement.
        assert await pool.select_one("SELECT pg_backend_pid()") == (last_pid,)
        assert pool.stats().idle == 2


async def test_pipelines_onto_least_stalled_connection_at_max_size(dsn: str) -> None:
    async with await fastpg.create_pool(
        dsn,
        min_size=2,
        max_size=2,
        max_inflight_per_conn=4,
    ) as pool:
        slow = asyncio.create_task(_pid_of_sleeper(pool, 0.4))
        await asyncio.sleep(0.05)
        medium = asyncio.create_task(_pid_of_sleeper(pool, 0.1))
        await asyncio.sleep(0.02)
        # Both connections are busy; the one behind the younger statement is preferred.
        (pid,) = await pool.select_one("SELECT pg_backend_pid()")
        assert pid == await medium
        assert pid != await slow


async def test_avoids_connection_behind_known_slow_statement(dsn: str) -> None:
    slow = "SELECT pg_backend_pid(), pg_sleep(0.2)"
    async with await fastpg.create_pool(dsn, min_size=2, max_size=2) as pool:
        await pool.select_one(slow)  # the pool learns that this statement takes ~200 ms
        unknown = asyncio.create_task(pool.select_one("SELECT pg_backend_pid(), pg_sleep(0.1)"))
        await asyncio.sleep(0.03)
        known = asyncio.create_task(pool.select_one(slow))
        await asyncio.sleep(0.005)
        # Both connections are busy: one for 35 ms with a statement of unknown cost, the
        # other just started one expected to take 200 ms. The first is the better bet.
        (pid,) = await pool.select_one("SELECT pg_backend_pid()")
        assert pid == (await unknown)[0]
        assert pid != (await known)[0]


async def test_age_counts_from_the_oldest_statement_still_in_flight(dsn: str) -> None:
    sleep = "SELECT pg_backend_pid(), pg_sleep($1)"
    async with await fastpg.create_pool(
        dsn,
        min_size=2,
        max_size=2,
        max_inflight_per_conn=4,
    ) as pool:
        await pool.select_one(sleep, 0.2)  # the pool learns that `sleep` takes ~200 ms
        first = asyncio.create_task(pool.select_one("SELECT pg_backend_pid(), pg_sleep(0.3)"))
        await asyncio.sleep(0.1)
        overrun = asyncio.create_task(pool.select_one(sleep, 0.6))  # on the other connection
        await asyncio.sleep(0.05)
        # Both busy: 150 ms into an unknown statement beats 200 ms expected, so this one is
        # pipelined behind `first`.
        behind = asyncio.create_task(pool.select_one("SELECT pg_backend_pid(), pg_sleep(0.2)"))
        await asyncio.sleep(0.25)
        # `first` is done. Its connection is 250 ms into `behind`, the other one 300 ms into
        # `overrun` (scored by `first`'s send time, the first would look 400 ms busy).
        (pid,) = await pool.select_one("SELECT pg_backend_pid()")
        assert pid == (await first)[0] == (await behind)[0]
        assert pid != (await overrun)[0]


@pytest.mark.parametrize("cap", [1, 3])
async def test_cap_and_fifo_waiting(dsn: str, cap: int) -> None:
    async with await fastpg.create_pool(
        dsn,
        min_size=1,
        max_size=1,
        max_inflight_per_conn=cap,
    ) as pool:
        sleepers = [
            asyncio.create_task(pool.select_one("SELECT pg_sleep(0.1)")) for _ in range(cap)
        ]
        await asyncio.sleep(0.02)
        assert pool.stats().in_flight == cap
        started: list[int] = []
        waiters = [asyncio.create_task(_record(pool, i, started)) for i in range(6)]
        await asyncio.sleep(0.02)
        stats = pool.stats()
        assert (stats.in_flight, stats.waiting) == (cap, 6)
        await asyncio.gather(*sleepers, *waiters)
        assert pool.stats().waiting == 0
        # Slots are handed out first come, first served. (Statements pipelined together on
        # one connection may complete in any order, so only cap=1 shows the order directly.)
        assert started == list(range(6)) if cap == 1 else sorted(started) == list(range(6))


async def _record(pool: fastpg.Pool, i: int, done: list[int]) -> None:
    (value,) = await pool.select_one("SELECT $1::int", i)
    done.append(value)


async def test_grows_to_max_size_and_no_further(dsn: str) -> None:
    async with await fastpg.create_pool(
        dsn,
        min_size=1,
        max_size=3,
        max_inflight_per_conn=8,
    ) as pool:
        assert pool.stats().size == 1
        tasks = [asyncio.create_task(_pid_of_sleeper(pool, 0.2)) for _ in range(10)]
        await asyncio.sleep(0)
        assert pool.stats().size + pool.stats().opening == 3
        pids = set(await asyncio.gather(*tasks))
        assert pool.stats().size == 3
        # The statements that arrived before the new connections opened were pipelined.
        assert 1 <= len(pids) <= 3


async def test_cap_one_is_one_statement_per_connection(dsn: str) -> None:
    async with await fastpg.create_pool(
        dsn,
        min_size=3,
        max_size=3,
        max_inflight_per_conn=1,
    ) as pool:
        pids = await asyncio.gather(*(_pid_of_sleeper(pool, 0.1) for _ in range(3)))
        assert len(set(pids)) == 3


async def test_cancelled_shared_query_keeps_its_slot_until_the_server_is_done(dsn: str) -> None:
    async with await fastpg.create_pool(dsn, min_size=1, max_size=1) as pool:
        task = asyncio.create_task(pool.select_one("SELECT pg_sleep(0.3)"))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Still running server-side, so the connection is still busy...
        assert pool.stats().in_flight == 1
        async with asyncio.timeout(2):
            async with pool.acquire():  # ...and only idle once the server finished it
                assert pool.stats().in_flight == 0
        assert await pool.select_one("SELECT 1") == (1,)


async def test_cancelled_shared_query_keeps_its_connection_busy(dsn: str) -> None:
    async with await fastpg.create_pool(dsn, min_size=2, max_size=2) as pool:
        task = asyncio.create_task(pool.select_one("SELECT pg_sleep(0.3)"))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        stats = pool.stats()
        assert (stats.idle, stats.in_flight) == (1, 1)
        # Not pipelined behind the cancelled statement: the other connection answers.
        async with asyncio.timeout(0.2):
            assert await pool.select_one("SELECT 1") == (1,)
        assert pool.stats().idle == 1
        await _settle(pool)
        stats = pool.stats()
        assert (stats.size, stats.idle, stats.in_flight) == (2, 2, 0)


async def test_cancelled_waiting_query_leaves_no_trace(dsn: str) -> None:
    async with await fastpg.create_pool(
        dsn,
        min_size=1,
        max_size=1,
        max_inflight_per_conn=1,
    ) as pool:
        sleeper = asyncio.create_task(pool.select_one("SELECT pg_sleep(0.1)"))
        await asyncio.sleep(0.02)
        waiter = asyncio.create_task(pool.select_one("SELECT 1"))
        await asyncio.sleep(0.01)
        assert pool.stats().waiting == 1
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert pool.stats().waiting == 0
        await sleeper
        assert await pool.select_one("SELECT 2") == (2,)


@pytest.mark.usefixtures("sequence")
async def test_waiting_statements_run_in_arrival_order(dsn: str) -> None:
    next_value = "SELECT nextval('pool_seq')"
    async with await fastpg.create_pool(
        dsn,
        min_size=1,
        max_size=1,
        max_inflight_per_conn=3,
    ) as pool:
        assert await pool.select_one(next_value) == (1,)  # prepared: none overtakes another
        sleepers = [asyncio.create_task(pool.select_one("SELECT pg_sleep(0.05)")) for _ in range(3)]
        await asyncio.sleep(0.02)
        waiters = [asyncio.create_task(pool.select_one(next_value)) for _ in range(9)]
        await asyncio.sleep(0.01)
        assert pool.stats().waiting == 9
        # Each freed slot goes to the first waiter, and its statement is sent at once: the
        # server runs them in arrival order, though three are pipelined at a time.
        assert await asyncio.gather(*waiters) == [(i,) for i in range(2, 11)]
        await asyncio.gather(*sleepers)


async def test_waiting_statement_is_sent_by_the_statement_that_frees_its_slot(
    dsn: str,
    conn: fastpg.Connection,
) -> None:
    async with await fastpg.create_pool(
        dsn,
        min_size=1,
        max_size=1,
        max_inflight_per_conn=1,
    ) as pool:
        await pool.select_one("SELECT clock_timestamp()")  # prepared: no Parse round trip
        waiter = asyncio.create_task(pool.select_one("SELECT clock_timestamp()"))
        # Takes the slot first (the task hasn't started yet): the waiter queues behind it.
        await pool.select_one("SELECT pg_sleep(0.05)")
        # Block the loop before the waiting task runs again: only a statement already sent
        # (by the completion above, not by its task) runs on the server meanwhile.
        time.sleep(0.2)
        (ran_at,) = await waiter
        (now,) = await conn.select_one("SELECT clock_timestamp()")
        assert now - ran_at >= dt.timedelta(seconds=0.15)


async def test_waiting_statements_errors_reach_their_callers(dsn: str) -> None:
    async with await fastpg.create_pool(
        dsn,
        min_size=1,
        max_size=1,
        max_inflight_per_conn=1,
    ) as pool:
        sleeper = asyncio.create_task(pool.select_one("SELECT pg_sleep(0.05)"))
        await asyncio.sleep(0.02)
        direct = pool.select_one("SELECT 1 AS id", into=Positive, strategy="direct")
        waiters: list[Awaitable[object]] = [
            asyncio.create_task(direct),
            asyncio.create_task(pool.select_one("SELEC 1")),
            asyncio.create_task(pool.select_one("SELECT 0 AS id", into=Positive)),
            asyncio.create_task(pool.select_one("SELECT 2 AS id", into=Positive)),
            asyncio.create_task(pool.execute("SELECT generate_series(1, 3)")),
            asyncio.create_task(pool.execute_script("SELECT 1; SELECT 2")),
            asyncio.create_task(pool.select_many("SELECT 3 AS id, 'c' AS v", into=Item)),
        ]
        await asyncio.sleep(0.01)
        assert pool.stats().waiting == len(waiters)
        results = await asyncio.gather(*waiters, return_exceptions=True)
        await sleeper  # the statement whose completion sent the first waiters is unaffected
        assert isinstance(results[0], fastpg.UnsupportedTypeError)  # failed before it was sent
        assert isinstance(results[1], errors.SQLSyntaxError)  # failed on the server
        assert isinstance(results[2], fastpg.DecodeError)  # failed decoding
        assert results[3:] == [Positive(2), 3, None, [Item(3, "c")]]
        stats = pool.stats()
        assert (stats.idle, stats.in_flight, stats.waiting) == (1, 0, 0)


async def test_cancelled_waiter_keeps_its_slot_once_sent(dsn: str) -> None:
    async with await fastpg.create_pool(
        dsn,
        min_size=1,
        max_size=1,
        max_inflight_per_conn=1,
    ) as pool:
        waiter = asyncio.create_task(pool.select_one("SELECT pg_sleep(0.2)"))
        await pool.select_one("SELECT pg_sleep(0.05)")  # its completion sends the waiter's
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        # Sent, so still running server-side: its slot stays taken until the server is done.
        stats = pool.stats()
        assert (stats.idle, stats.in_flight, stats.waiting) == (0, 1, 0)
        async with asyncio.timeout(2), pool.acquire():
            assert pool.stats().in_flight == 0
        assert await pool.select_one("SELECT 1") == (1,)
        stats = pool.stats()
        assert (stats.idle, stats.in_flight, stats.waiting) == (1, 0, 0)


# --- exclusive mode -------------------------------------------------------------------------


async def test_acquire_is_exclusive(dsn: str) -> None:
    async with await fastpg.create_pool(dsn, min_size=1, max_size=1) as pool:
        async with pool.acquire() as conn:
            assert isinstance(conn, fastpg.Connection)
            (pid,) = await conn.select_one("SELECT pg_backend_pid()")
            shared = asyncio.create_task(pool.select_one("SELECT pg_backend_pid()"))
            other = asyncio.create_task(_acquired_pid(pool))
            await asyncio.sleep(0.05)
            assert not shared.done()
            assert not other.done()
            stats = pool.stats()
            assert (stats.acquired, stats.waiting, stats.waiting_acquire) == (1, 1, 1)
        assert await shared == (pid,)
        assert await other == pid


async def _acquired_pid(pool: fastpg.Pool) -> int:
    async with pool.acquire() as conn:
        (pid,) = await conn.select_one("SELECT pg_backend_pid()")
        return pid


async def test_acquire_drains_a_busy_connection_at_max_size(dsn: str) -> None:
    async with await fastpg.create_pool(
        dsn,
        min_size=1,
        max_size=1,
        max_inflight_per_conn=8,
    ) as pool:
        first = asyncio.create_task(pool.select_one("SELECT pg_sleep(0.1)"))
        await asyncio.sleep(0.02)
        acquired = asyncio.create_task(_acquired_pid(pool))
        await asyncio.sleep(0.01)
        # Draining: new shared statements wait instead of keeping the connection busy.
        later = asyncio.create_task(pool.select_one("SELECT 1"))
        await asyncio.sleep(0.01)
        assert pool.stats().waiting == 1
        await first
        await acquired
        assert await later == (1,)
        await _settle(pool)
        assert pool.stats().idle == 1


async def test_acquire_takes_the_busy_connection_that_frees_first(dsn: str) -> None:
    async with await fastpg.create_pool(
        dsn,
        min_size=2,
        max_size=2,
        max_inflight_per_conn=8,
    ) as pool:
        long = asyncio.create_task(_pid_of_sleeper(pool, 0.4))
        short = asyncio.create_task(_pid_of_sleeper(pool, 0.1))
        await asyncio.sleep(0.02)
        acquired = asyncio.create_task(_acquired_pid(pool))
        await asyncio.sleep(0.02)
        stats = pool.stats()
        assert (stats.idle, stats.waiting_acquire) == (0, 1)
        assert await acquired == await short
        assert not long.done()
        await long
        await _settle(pool)
        stats = pool.stats()
        assert (stats.size, stats.idle, stats.acquired) == (2, 2, 0)


async def test_idle_count_follows_acquire_and_release(dsn: str) -> None:
    async with await fastpg.create_pool(dsn, min_size=3, max_size=3) as pool:
        assert pool.stats().idle == 3
        async with pool.acquire():
            assert (pool.stats().idle, pool.stats().acquired) == (2, 1)
            async with pool.transaction() as tx:
                assert (pool.stats().idle, pool.stats().acquired) == (1, 2)
                assert await pool.select_one("SELECT 1") == (1,)  # on the last idle one
                assert await tx.select_one("SELECT 2") == (2,)
                assert pool.stats().idle == 1
            assert (pool.stats().idle, pool.stats().acquired) == (2, 1)
        assert (pool.stats().idle, pool.stats().acquired) == (3, 0)
        async with pool.acquire() as conn:
            await conn.close()
        stats = pool.stats()
        assert (stats.size + stats.opening, stats.idle, stats.acquired) == (3, 2, 0)
        await _settle(pool)
        assert pool.stats().idle == 3


async def test_acquire_grows_the_pool(dsn: str) -> None:
    pool = await fastpg.create_pool(dsn, min_size=1, max_size=2)
    async with pool, pool.acquire() as a, pool.acquire() as b:
        (pid_a,) = await a.select_one("SELECT pg_backend_pid()")
        (pid_b,) = await b.select_one("SELECT pg_backend_pid()")
        assert pid_a != pid_b
        assert pool.stats().acquired == 2


async def test_cancelled_acquire_waiter(dsn: str) -> None:
    async with await fastpg.create_pool(dsn, min_size=1, max_size=1) as pool:
        async with pool.acquire():
            waiter = asyncio.create_task(_acquired_pid(pool))
            await asyncio.sleep(0.02)
            assert pool.stats().waiting_acquire == 1
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            assert pool.stats().waiting_acquire == 0
        async with asyncio.timeout(1):
            await _acquired_pid(pool)
        assert pool.stats().acquired == 0


async def test_cancelled_acquire_waiter_undrains_the_connection(dsn: str) -> None:
    async with await fastpg.create_pool(dsn, min_size=1, max_size=1) as pool:
        sleeper = asyncio.create_task(pool.select_one("SELECT pg_sleep(0.1)"))
        await asyncio.sleep(0.02)
        acquiring = asyncio.create_task(_acquired_pid(pool))
        await asyncio.sleep(0.01)  # the busy connection is draining for it
        acquiring.cancel()
        with pytest.raises(asyncio.CancelledError):
            await acquiring
        # Back in shared rotation: a new statement is pipelined onto it instead of waiting.
        shared = asyncio.create_task(pool.select_one("SELECT 1"))
        await asyncio.sleep(0.01)
        stats = pool.stats()
        assert (stats.in_flight, stats.waiting, stats.waiting_acquire) == (2, 0, 0)
        assert await shared == (1,)
        await sleeper


async def test_drained_connection_rejoins_shared_rotation_when_the_acquire_is_served_elsewhere(
    dsn: str,
) -> None:
    async with await fastpg.create_pool(dsn, min_size=2, max_size=2) as pool:
        short = asyncio.create_task(pool.select_one("SELECT pg_sleep(0.1)"))
        long = asyncio.create_task(pool.select_one("SELECT pg_sleep(0.4)"))
        await asyncio.sleep(0.02)
        # Both busy at max_size: the acquire drains one, and takes whichever frees first.
        async with asyncio.timeout(1), pool.acquire():
            assert short.done()
            assert not long.done()
            # No acquire waits any more, so no connection stays drained: a new statement is
            # pipelined behind the long one instead of waiting for it.
            shared = asyncio.create_task(pool.select_one("SELECT 1"))
            await asyncio.sleep(0.01)
            stats = pool.stats()
            assert (stats.in_flight, stats.waiting, stats.waiting_acquire) == (2, 0, 0)
            assert await shared == (1,)
        await long


async def test_session_is_reset_on_release(dsn: str) -> None:
    async with await fastpg.create_pool(dsn, min_size=1, max_size=1) as pool:
        async with pool.acquire() as conn:
            (pid,) = await conn.select_one("SELECT pg_backend_pid()")
            await conn.execute_script(
                "SET work_mem = '1MB'; SET ROLE pg_read_all_data;"
                " CREATE TEMP TABLE pool_temp (a int); SELECT pg_advisory_lock(4242)",
            )
            assert await conn.select_one("SELECT 1 + $1::int", 1) == (2,)  # prepared + cached
        async with pool.acquire() as conn:
            assert await conn.select_one("SELECT pg_backend_pid()") == (pid,)  # same session
            assert await conn.select_one(
                "SELECT current_setting('work_mem'), current_user,"
                " to_regclass('pg_temp.pool_temp') IS NULL,"
                " (SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = $1)",
                pid,
            ) == ("32MB", "fastpg", True, 0)
            (prepared,) = await conn.select_one("SELECT count(*) FROM pg_prepared_statements")
            assert prepared > 0  # prepared statements survive the reset


@pytest.mark.usefixtures("items")
async def test_open_transaction_is_rolled_back_on_release(dsn: str) -> None:
    async with await fastpg.create_pool(dsn, min_size=1, max_size=1) as pool:
        async with pool.acquire() as conn:
            await conn.execute_script("BEGIN; INSERT INTO pool_items VALUES (1, 'a')")
        async with pool.acquire() as conn:  # failed transaction
            await conn.execute_script("BEGIN")
            with pytest.raises(errors.DataError):
                await conn.select_one("SELECT 1 / 0")
        assert await pool.select_one("SELECT count(*) FROM pool_items") == (0,)
        assert await pool.execute("INSERT INTO pool_items VALUES (2, 'b')") == 1
        async with pool.acquire() as conn:  # autocommit again
            assert await conn.select_one("SELECT count(*) FROM pool_items") == (1,)


async def test_closed_connection_is_not_returned(dsn: str) -> None:
    async with await fastpg.create_pool(dsn, min_size=1, max_size=1) as pool:
        async with pool.acquire() as conn:
            (pid,) = await conn.select_one("SELECT pg_backend_pid()")
            await conn.close()
        assert pool.stats().size == 0
        assert await pool.select_one("SELECT pg_backend_pid()") != (pid,)


# --- transactions ---------------------------------------------------------------------------


@pytest.mark.usefixtures("items")
async def test_pool_transaction_commit_and_rollback(dsn: str) -> None:
    async with await fastpg.create_pool(dsn, min_size=1, max_size=2) as pool:
        async with pool.transaction() as tx:
            assert isinstance(tx, fastpg.Transaction)
            await tx.execute("INSERT INTO pool_items VALUES ($1, $2)", 1, "a")
            # Not visible outside the transaction yet.
            assert await pool.select_one("SELECT count(*) FROM pool_items") == (0,)
        assert await pool.select_many("SELECT id, v FROM pool_items", into=Item) == [Item(1, "a")]
        with pytest.raises(RuntimeError, match="error"):
            await _insert_and_raise(pool)
        assert await pool.select_one("SELECT count(*) FROM pool_items") == (1,)
        assert pool.stats().acquired == 0


async def _insert_and_raise(pool: fastpg.Pool) -> None:
    async with pool.transaction() as tx:
        await tx.execute("INSERT INTO pool_items VALUES ($1, $2)", 2, "b")
        msg = "error"
        raise RuntimeError(msg)


async def _insert_duplicate(tx: fastpg.Transaction) -> None:
    async with tx.savepoint():
        await tx.execute("INSERT INTO pool_items VALUES (2, 'b')")
        await tx.execute("INSERT INTO pool_items VALUES (1, 'dup')")


async def _insert_then_miss(tx: fastpg.Transaction) -> None:
    async with tx.savepoint():
        await tx.execute("INSERT INTO pool_items VALUES (4, 'd')")
        await tx.select_one("SELECT 1 FROM pool_items WHERE id = 99")


@pytest.mark.usefixtures("items")
async def test_savepoints(dsn: str) -> None:
    async with await fastpg.create_pool(dsn) as pool:
        async with pool.transaction() as tx:
            await tx.execute("INSERT INTO pool_items VALUES (1, 'a')")
            with pytest.raises(fastpg.UniqueViolationError):
                await _insert_duplicate(tx)
            async with tx.savepoint() as sp:
                await sp.execute("INSERT INTO pool_items VALUES (3, 'c')")
                with pytest.raises(LookupError):  # nested savepoint
                    await _insert_then_miss(sp)
        assert await pool.select_many("SELECT id FROM pool_items ORDER BY id") == [(1,), (3,)]


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({}, ("read committed", "off", "off")),
        ({"isolation": "serializable"}, ("serializable", "off", "off")),
        ({"isolation": "repeatable_read", "readonly": True}, ("repeatable read", "on", "off")),
        (
            {"isolation": "serializable", "readonly": True, "deferrable": True},
            ("serializable", "on", "on"),
        ),
    ],
)
async def test_transaction_options(
    dsn: str,
    options: dict[str, Any],
    expected: tuple[str, str, str],
) -> None:
    sql = (
        "SELECT current_setting('transaction_isolation'),"
        " current_setting('transaction_read_only'), current_setting('transaction_deferrable')"
    )
    async with await fastpg.create_pool(dsn) as pool:
        async with pool.transaction(**options) as tx:
            assert await tx.select_one(sql) == expected
        async with await fastpg.connect(dsn) as conn, conn.transaction(**options) as tx:
            assert await tx.select_one(sql) == expected


async def _insert_readonly(pool: fastpg.Pool) -> None:
    async with pool.transaction(readonly=True) as tx:
        await tx.execute("INSERT INTO pool_items VALUES (1, 'a')")


@pytest.mark.usefixtures("items")
async def test_readonly_transaction_rejects_writes(dsn: str) -> None:
    async with await fastpg.create_pool(dsn) as pool:
        with pytest.raises(fastpg.DatabaseError) as exc:
            await _insert_readonly(pool)
        assert exc.value.sqlstate == "25006"
        assert await pool.select_one("SELECT count(*) FROM pool_items") == (0,)


async def test_invalid_isolation_level(dsn: str) -> None:
    untyped_caller: Any = "isolation"  # what an untyped caller could pass
    async with await fastpg.connect(dsn) as conn:
        with pytest.raises(ValueError, match="invalid isolation level"):
            conn.transaction(isolation=untyped_caller)


async def test_pool_transaction_resets_session(dsn: str) -> None:
    async with await fastpg.create_pool(dsn, min_size=1, max_size=1) as pool:
        async with pool.transaction() as tx:
            await tx.execute_script("SET work_mem = '1MB'")  # session-level, survives COMMIT
        assert await pool.select_one("SELECT current_setting('work_mem')") == ("32MB",)


async def _swallow_error_then_commit(conn: fastpg.Connection) -> None:
    async with conn.transaction() as tx:
        await tx.execute("INSERT INTO pool_items VALUES (1, 'a')")
        with pytest.raises(errors.DataError):
            await tx.select_one("SELECT 1 / 0")


@pytest.mark.usefixtures("items")
async def test_commit_of_aborted_transaction_raises(conn: fastpg.Connection) -> None:
    with pytest.raises(fastpg.DatabaseError) as exc:
        await _swallow_error_then_commit(conn)
    assert exc.value.sqlstate == "25P02"
    assert await conn.select_one("SELECT count(*) FROM pool_items") == (0,)
    assert await conn.select_one("SELECT now() = statement_timestamp()") == (False,)
    async with conn.transaction() as tx:  # the connection is out of the failed transaction
        await tx.execute("INSERT INTO pool_items VALUES (2, 'b')")
    assert await conn.select_one("SELECT count(*) FROM pool_items") == (1,)


async def _enter(tx: fastpg.Transaction) -> None:
    async with tx:
        pass


async def _insert_and_divide(conn: fastpg.Connection) -> None:
    async with conn.transaction() as tx:
        await tx.execute("INSERT INTO pool_items VALUES (2, 'b')")
        _ = 1 / 0


@pytest.mark.usefixtures("items")
async def test_connection_transaction(conn: fastpg.Connection) -> None:
    async with conn.transaction() as tx:
        assert tx.active
        await tx.execute("INSERT INTO pool_items VALUES (1, 'a')")
        with pytest.raises(fastpg.InterfaceError, match="already active"):
            conn.transaction()
    assert not tx.active
    with pytest.raises(fastpg.InterfaceError, match="only be entered once"):
        await _enter(tx)
    with pytest.raises(ZeroDivisionError):
        await _insert_and_divide(conn)
    assert await conn.select_many("SELECT id FROM pool_items") == [(1,)]


@pytest.mark.usefixtures("items")
async def test_cancelled_transaction_rolls_back(dsn: str) -> None:
    async with await fastpg.create_pool(dsn, min_size=1, max_size=1) as pool:
        task = asyncio.create_task(_insert_then_sleep(pool))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await pool.select_one("SELECT count(*) FROM pool_items") == (0,)
        assert pool.stats().size == 1


async def _insert_then_sleep(pool: fastpg.Pool) -> None:
    async with pool.transaction() as tx:
        await tx.execute("INSERT INTO pool_items VALUES (1, 'a')")
        await tx.select_one("SELECT pg_sleep(0.2)")


# --- bulk writes ----------------------------------------------------------------------------


@pytest.mark.usefixtures("items")
async def test_bulk_writes(dsn: str) -> None:
    async with await fastpg.create_pool(dsn, min_size=1, max_size=2) as pool:
        written = await asyncio.gather(
            pool.copy_in("pool_items", ((i, "c") for i in range(100_000))),
            pool.insert_many("pool_items", [Item(-1, "i"), Item(-2, "i")]),
            pool.execute_many("INSERT INTO pool_items VALUES ($1, $2)", [(-3, "e")]),
            pool.select_one("SELECT 1"),
        )
        assert written == [100_000, 2, 1, (1,)]
        assert await pool.select_one("SELECT count(*), min(id) FROM pool_items") == (100_003, -3)
        stats = pool.stats()
        assert (stats.acquired, stats.waiting_acquire) == (0, 0)


# --- failures and lifecycle -----------------------------------------------------------------


async def test_terminated_connection_is_replaced(dsn: str, conn: fastpg.Connection) -> None:
    async with await fastpg.create_pool(dsn, min_size=1, max_size=1) as pool:
        (pid,) = await pool.select_one("SELECT pg_backend_pid()")
        await conn.select_one("SELECT pg_terminate_backend($1)", pid)
        await asyncio.sleep(0.1)  # let the EOF arrive
        (new_pid,) = await pool.select_one("SELECT pg_backend_pid()")
        assert new_pid != pid
        assert pool.stats().size == 1


async def _top_pid_terminated(pool: fastpg.Pool, conn: fastpg.Connection) -> int:
    """Terminate the connection the pool would hand out next; returns its pid."""
    (pid,) = await pool.select_one("SELECT pg_backend_pid()")
    await conn.select_one("SELECT pg_terminate_backend($1)", pid)
    await asyncio.sleep(0.1)  # let the EOF arrive
    return pid


async def test_dead_idle_connection_is_skipped_by_shared_statements(
    dsn: str,
    conn: fastpg.Connection,
) -> None:
    async with await fastpg.create_pool(dsn, min_size=2, max_size=2) as pool:
        dead = await _top_pid_terminated(pool, conn)
        assert await pool.select_one("SELECT pg_backend_pid()") != (dead,)
        await _settle(pool)
        stats = pool.stats()
        assert (stats.size, stats.idle) == (2, 2)


async def test_dead_idle_connection_is_skipped_by_acquire(
    dsn: str,
    conn: fastpg.Connection,
) -> None:
    async with await fastpg.create_pool(dsn, min_size=2, max_size=2) as pool:
        dead = await _top_pid_terminated(pool, conn)
        assert await _acquired_pid(pool) != dead
        await _settle(pool)
        stats = pool.stats()
        assert (stats.size, stats.idle) == (2, 2)


async def test_in_flight_query_fails_when_its_connection_dies(
    dsn: str,
    conn: fastpg.Connection,
) -> None:
    async with await fastpg.create_pool(dsn, min_size=1, max_size=1) as pool:
        (pid,) = await pool.select_one("SELECT pg_backend_pid()")
        sleeping = asyncio.create_task(pool.select_one("SELECT pg_sleep(30)"))
        await asyncio.sleep(0.1)
        await conn.select_one("SELECT pg_terminate_backend($1)", pid)
        async with asyncio.timeout(5):
            with pytest.raises((errors.OperationalError, fastpg.ConnectionClosedError)):
                await sleeping
        assert await pool.select_one("SELECT pg_backend_pid()") != (pid,)


async def test_pipelined_queries_fail_cleanly_when_their_connection_dies(
    dsn: str,
    conn: fastpg.Connection,
) -> None:
    async with await fastpg.create_pool(dsn, min_size=1, max_size=1) as pool:
        (pid,) = await pool.select_one("SELECT pg_backend_pid()")
        sleeping = [asyncio.create_task(pool.select_one("SELECT pg_sleep(30)")) for _ in range(3)]
        await asyncio.sleep(0.1)
        assert pool.stats().in_flight == 3
        await conn.select_one("SELECT pg_terminate_backend($1)", pid)
        async with asyncio.timeout(5):
            results = await asyncio.gather(*sleeping, return_exceptions=True)
        # The first failure drops the connection; the others still finish their bookkeeping.
        closed = (errors.OperationalError, fastpg.ConnectionClosedError)
        assert all(isinstance(result, closed) for result in results)
        assert await pool.select_one("SELECT pg_backend_pid()") != (pid,)
        await _settle(pool)
        stats = pool.stats()
        assert (stats.size, stats.idle, stats.in_flight) == (1, 1, 0)


async def test_unreachable_server_fails_waiters(dsn: str) -> None:
    bad = "postgresql://fastpg:fastpg@127.0.0.1:1/fastpg"
    with pytest.raises(fastpg.InterfaceError):
        await fastpg.create_pool(bad)
    async with await fastpg.create_pool(bad, min_size=0) as pool:
        async with asyncio.timeout(5):
            with pytest.raises(fastpg.InterfaceError):
                await pool.select_one("SELECT 1")
            with pytest.raises(fastpg.InterfaceError):
                async with pool.acquire():
                    pass
    async with await fastpg.create_pool(dsn, min_size=0) as pool:
        assert pool.stats().size == 0
        assert await pool.select_one("SELECT 1") == (1,)


@pytest.mark.parametrize(
    ("min_size", "max_size", "cap"),
    [(2, 1, 1), (-1, 1, 1), (0, 0, 1), (1, 1, 0)],
)
async def test_invalid_sizes(dsn: str, min_size: int, max_size: int, cap: int) -> None:
    with pytest.raises(ValueError, match=">="):
        await fastpg.create_pool(
            dsn,
            min_size=min_size,
            max_size=max_size,
            max_inflight_per_conn=cap,
        )


async def test_close_is_graceful(dsn: str) -> None:
    pool = await fastpg.create_pool(dsn, min_size=1, max_size=1, max_inflight_per_conn=1)
    in_flight = asyncio.create_task(pool.select_one("SELECT pg_sleep(0.1), 1"))
    await asyncio.sleep(0.02)
    waiting = asyncio.create_task(pool.select_one("SELECT 2"))
    await asyncio.sleep(0.01)
    await pool.close()
    assert pool.closed
    assert (await in_flight)[1] == 1
    with pytest.raises(fastpg.ConnectionClosedError):
        await waiting
    with pytest.raises(fastpg.ConnectionClosedError):
        await pool.select_one("SELECT 1")
    with pytest.raises(fastpg.ConnectionClosedError):
        async with pool.acquire():
            pass
    await pool.close()  # idempotent
    assert pool.stats().size == 0


async def test_close_waits_for_acquired_connections(dsn: str) -> None:
    pool = await fastpg.create_pool(dsn, min_size=2, max_size=2)
    released = asyncio.Event()
    holder = asyncio.create_task(_hold(pool, released))
    await asyncio.sleep(0.02)
    closing = asyncio.create_task(pool.close())
    await asyncio.sleep(0.05)
    assert not closing.done()
    assert pool.stats().size == 1  # the idle one is already gone
    released.set()
    assert await holder == (1,)
    async with asyncio.timeout(2):
        await closing
    assert pool.stats().size == 0


async def _hold(pool: fastpg.Pool, released: asyncio.Event) -> tuple[object, ...]:
    async with pool.acquire() as conn:
        await released.wait()
        return await conn.select_one("SELECT 1")


async def test_no_connection_leaks(dsn: str, conn: fastpg.Connection) -> None:
    tagged = f"{dsn}?application_name=fastpg_pool_leak_test"
    async with await fastpg.create_pool(tagged, min_size=2, max_size=6) as pool:
        await asyncio.gather(*(_pid_of_sleeper(pool, 0.05) for _ in range(20)))
        async with pool.acquire(), pool.transaction():
            pass
        task = asyncio.create_task(pool.select_one("SELECT pg_sleep(0.2)"))
        await asyncio.sleep(0.02)
        task.cancel()
    sql = "SELECT count(*) FROM pg_stat_activity WHERE application_name = 'fastpg_pool_leak_test'"
    async with asyncio.timeout(5):
        while (await conn.select_one(sql))[0]:
            await asyncio.sleep(0.05)


async def test_max_lifetime_recycles_idle_connections(dsn: str) -> None:
    async with await fastpg.create_pool(dsn, min_size=1, max_size=1, max_lifetime=0.1) as pool:
        (pid,) = await pool.select_one("SELECT pg_backend_pid()")
        async with asyncio.timeout(5):
            while await pool.select_one("SELECT pg_backend_pid()") == (pid,):
                await asyncio.sleep(0.05)


async def test_max_lifetime_retires_a_busy_connection_once_idle(dsn: str) -> None:
    async with await fastpg.create_pool(dsn, min_size=1, max_size=1, max_lifetime=0.2) as pool:
        sleeper = asyncio.create_task(_pid_of_sleeper(pool, 0.5))
        await asyncio.sleep(0.35)  # expired while busy: it takes no new statements
        later = asyncio.create_task(pool.select_one("SELECT pg_backend_pid()"))
        await asyncio.sleep(0.01)
        stats = pool.stats()
        assert (stats.idle, stats.in_flight, stats.waiting) == (0, 1, 1)
        old_pid = await sleeper
        assert await later != (old_pid,)
        await _settle(pool)
        stats = pool.stats()
        assert (stats.size, stats.idle) == (1, 1)


async def test_max_idle_shrinks_to_min_size(dsn: str) -> None:
    async with await fastpg.create_pool(
        dsn,
        min_size=1,
        max_size=3,
        max_inflight_per_conn=2,
        max_idle=0.2,
    ) as pool:
        await asyncio.gather(*(_pid_of_sleeper(pool, 0.05) for _ in range(6)))
        assert pool.stats().size == 3
        async with asyncio.timeout(5):
            while pool.stats().size > 1:
                await asyncio.sleep(0.05)


async def test_max_idle_shrinks_while_light_load_continues(dsn: str) -> None:
    async with await fastpg.create_pool(
        dsn,
        min_size=1,
        max_size=3,
        max_inflight_per_conn=2,
        max_idle=0.2,
    ) as pool:
        await asyncio.gather(*(_pid_of_sleeper(pool, 0.05) for _ in range(6)))
        assert pool.stats().size == 3
        # One statement at a time, more often than max_idle: it keeps reusing one
        # connection, so the other two age out.
        async with asyncio.timeout(5):
            while pool.stats().size > 1:
                assert await pool.select_one("SELECT 1") == (1,)
                await asyncio.sleep(0.02)


async def test_stats(dsn: str) -> None:
    async with await fastpg.create_pool(dsn, min_size=2, max_size=4) as pool:
        assert pool.stats() == fastpg.PoolStats(
            size=2,
            opening=0,
            idle=2,
            acquired=0,
            in_flight=0,
            waiting=0,
            waiting_acquire=0,
        )
        async with pool.acquire():
            stats = pool.stats()
            assert (stats.idle, stats.acquired) == (1, 1)
