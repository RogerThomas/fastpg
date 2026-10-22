"""Cancellation at every await point, at many timings.

Each test cancels an operation after ``k`` event-loop iterations, for ``k`` drawn by a seeded
RNG (so a failure replays), then checks that the connection or pool is consistent: the next
query gets its own result, no transaction is left open, writes are all-or-nothing, the pool's
stats go back to idle, and the loop reports no orphaned futures or tasks.
"""

import asyncio
import gc
import random
import socket
from pathlib import Path
from typing import TYPE_CHECKING, Any

import msgspec
import pytest

import fastpg
from fastpg import errors

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Coroutine, Iterator

    from conftest import LoopErrors


class Row(msgspec.Struct):
    id: int
    v: str


class Checked(msgspec.Struct):
    """Needs validation, so it's decoded by the msgpack strategy (a coroutine in between)."""

    id: int
    v: str = msgspec.field(default="")

    def __post_init__(self) -> None:
        if self.id < 0:
            msg = "negative id"
            raise ValueError(msg)


@pytest.fixture
async def table(conn: fastpg.Connection) -> AsyncIterator[str]:
    """A fresh ``edge_cancel (id int PRIMARY KEY, v text)`` table; yields its name."""
    await conn.execute_script(
        "DROP TABLE IF EXISTS edge_cancel; CREATE TABLE edge_cancel (id int PRIMARY KEY, v text)",
    )
    yield "edge_cancel"
    await conn.execute_script("DROP TABLE edge_cancel")


@pytest.fixture
def rng() -> random.Random:
    return random.Random(20261016)  # noqa: S311 - reproducible timings, not cryptography


@pytest.fixture
def silent_server() -> Iterator[socket.socket]:
    """A listening socket that accepts connections (in the kernel) and never answers."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        yield listener


def _open_fds() -> int:
    """File descriptors open in this process, once unreachable objects are collected (an
    exception's traceback can keep a failed connect's frame alive in a reference cycle)."""
    gc.collect()
    return len(list(Path("/dev/fd").iterdir()))


async def _cancel_after[T](coro: Coroutine[Any, Any, T], iterations: int) -> asyncio.Task[T]:
    """Run ``coro`` as a task and cancel it after ``iterations`` loop iterations (unless it
    finished first); returns the finished task."""
    task = asyncio.create_task(coro)
    for _ in range(iterations):
        if task.done():
            break
        await asyncio.sleep(0)
    task.cancel()
    await asyncio.wait([task])
    return task


async def _connect_cancelled_after(dsn: str, iterations: int) -> None:
    """Start a connect and cancel it after ``iterations`` loop iterations (closing the
    connection if it was established first). Keeps no reference to the task: a cancelled
    task's traceback holds the frames of the connect it interrupted."""
    task = await _cancel_after(fastpg.connect(dsn), iterations)
    if not task.cancelled():
        await task.result().close()


async def _wait_until_alone(dsn: str, name: str) -> None:
    """Connect with ``dsn`` (tagged ``name``) until no other session has that tag."""
    query = "SELECT count(*) FROM pg_stat_activity WHERE application_name = $1"
    async with await fastpg.connect(dsn) as conn, asyncio.timeout(5):
        while (await conn.select_one(query, name))[0] > 1:
            await asyncio.sleep(0.01)


async def _connect_times_out(dsn: str, seconds: float) -> bool:
    """Whether connecting takes longer than ``seconds``; any connection made is closed.

    Run it as a task of its own: a task that ``asyncio.timeout`` cancelled keeps the frames
    it interrupted (the connect's, holding its native connection) referenced while it runs.
    """
    try:
        async with asyncio.timeout(seconds):
            conn = await fastpg.connect(dsn)
    except TimeoutError:
        return True
    await conn.close()
    return False


async def _assert_consistent(conn: fastpg.Connection, marker: int) -> None:
    """The next query gets its own result, outside any transaction."""
    assert not conn.closed
    assert await conn.select_one("SELECT $1::int", marker) == (marker,)
    # A simple-protocol statement outside a transaction block starts its own transaction,
    # so now() (the transaction's start) is its statement_timestamp(); inside one, it's older.
    await conn.execute_script(
        "SELECT (CASE WHEN now() = statement_timestamp() THEN '0'"
        " ELSE 'in a transaction' END)::int",
    )


async def _count(conn: fastpg.Connection, low: int, high: int) -> int:
    (count,) = await conn.select_one(
        "SELECT count(*)::int FROM edge_cancel WHERE id >= $1 AND id < $2",
        low,
        high,
    )
    return count


async def _transaction_insert(conn: fastpg.Connection, i: int) -> None:
    async with conn.transaction() as tx:
        await tx.execute("INSERT INTO edge_cancel VALUES ($1, 'tx')", i)
        await tx.select_one("SELECT $1::int", i)


async def _savepoint_insert(tx: fastpg.Transaction, i: int) -> None:
    async with tx.savepoint():
        await tx.execute("INSERT INTO edge_cancel VALUES ($1, 'sp')", i)


async def _pool_query(pool: fastpg.Pool, i: int) -> object:
    match i % 5:
        case 0:
            return await pool.select_one("SELECT $1::int", i)
        case 1:
            return await pool.select_one("SELECT $1::int AS id, 'v' AS v", i, into=Row)
        case 2:
            return await pool.select_one("SELECT $1::int AS id", i, into=Checked)
        case 3:
            return len(await pool.select_many("SELECT g FROM generate_series(1, 2000) g"))
        case _:
            return await pool.select_one("SELECT 1 / ($1::int % 2)", i)  # i odd: fine


def _check_pool_query(task: asyncio.Task[object], i: int) -> None:
    """The outcome of ``_pool_query(pool, i)``, unless it was cancelled."""
    if task.cancelled():
        return
    if i % 10 == 4:  # i % 5 == 4 and even: a division by zero
        assert isinstance(task.exception(), errors.DataError)
        return
    expected: dict[int, object] = {0: (i,), 1: Row(i, "v"), 2: Checked(i), 3: 2000, 4: (1,)}
    assert task.result() == expected[i % 5]


async def _pool_transaction(pool: fastpg.Pool, i: int) -> None:
    async with pool.transaction() as tx:
        await tx.execute("SET LOCAL application_name = 'edge-cancel'")
        await tx.execute("INSERT INTO edge_cancel VALUES ($1, 'pool')", i)


async def _pool_acquire(pool: fastpg.Pool, i: int) -> object:
    async with pool.acquire() as conn:
        await conn.execute_script("SET application_name = 'edge-cancel'")
        return await conn.select_one("SELECT $1::int", i)


def _bulk_write(
    target: fastpg.Connection | fastpg.Transaction | fastpg.Pool,
    method: str,
    low: int,
    rows: int,
) -> Coroutine[Any, Any, int]:
    """Write ``rows`` rows with ids from ``low`` by ``method``."""
    data = [(low + r, "v") for r in range(rows)]
    if method == "execute_many":
        return target.execute_many("INSERT INTO edge_cancel VALUES ($1, $2)", data)
    if method == "insert_many":
        return target.insert_many("edge_cancel", data)
    return target.copy_in("edge_cancel", iter(data))


async def _in_transaction(conn: fastpg.Connection) -> bool:
    try:
        await _assert_consistent(conn, 0)
    except errors.DataError:
        return True
    return False


async def _settled(pool: fastpg.Pool) -> fastpg.PoolStats:
    """The pool's stats once nothing is in flight, waiting, opening or checked out.

    Checked out included: a checkout cancelled inside its block is reset and given back in
    the background (`Pool._give_back`), so it's still acquired for a round trip after its
    task is done. (A connection that never came back still fails, after the timeout.)
    """
    async with asyncio.timeout(10):
        while (
            (s := pool.stats()).in_flight
            or s.waiting
            or s.waiting_acquire
            or s.opening
            or s.acquired
        ):
            await asyncio.sleep(0.01)
    return pool.stats()


# --- one connection -------------------------------------------------------------------------


async def test_select_one_cancelled_at_random_points(
    conn: fastpg.Connection,
    rng: random.Random,
    loop_errors: LoopErrors,
) -> None:
    queries = ["SELECT $1::int", "SELECT $1::int FROM pg_sleep(0.001)", "SELECT $1::int / 0"]
    for i in range(150):
        query = rng.choice(queries)
        task = await _cancel_after(conn.select_one(query, i), rng.randrange(6))
        if task.cancelled():
            pass  # the division by zero then fails with nobody waiting: nothing to report
        elif query.endswith("/ 0"):
            assert isinstance(task.exception(), errors.DataError)
        else:
            assert task.result() == (i,)
        await _assert_consistent(conn, i)
    assert loop_errors.messages() == []


async def test_select_many_cancelled_mid_stream(
    conn: fastpg.Connection,
    rng: random.Random,
    loop_errors: LoopErrors,
) -> None:
    query = "SELECT g, repeat('x', 50) FROM generate_series(1, 20000) g"
    cancelled = 0
    for i in range(40):
        task = await _cancel_after(conn.select_many(query, into=dict), rng.randrange(80))
        cancelled += task.cancelled()
        if not task.cancelled():
            assert len(task.result()) == 20000
        await _assert_consistent(conn, i)
    assert cancelled
    assert len(await conn.select_many(query)) == 20000
    assert loop_errors.messages() == []


@pytest.mark.parametrize("strategy", ["msgpack", "json"])
async def test_validated_fetch_cancelled_at_random_points(
    conn: fastpg.Connection,
    rng: random.Random,
    loop_errors: LoopErrors,
    strategy: fastpg.Strategy,
) -> None:
    for i in range(60):
        coro = conn.select_many(
            "SELECT g AS id FROM generate_series($1::int, 300) g",
            rng.choice([-1, 1]),  # -1: the decoder rejects a row
            into=Checked,
            strategy=strategy,
        )
        await _cancel_after(coro, rng.randrange(6))
        await _assert_consistent(conn, i)
    assert loop_errors.messages() == []


@pytest.mark.usefixtures("table")
@pytest.mark.parametrize("method", ["execute_many", "insert_many", "copy_in"])
@pytest.mark.timeout(60)
async def test_bulk_write_cancelled_at_random_points_is_all_or_nothing(
    conn: fastpg.Connection,
    rng: random.Random,
    loop_errors: LoopErrors,
    method: str,
) -> None:
    rows = 3000  # several chunks: cancels land at the start, mid-stream and at the end
    outcomes: set[str] = set()
    for i in range(40):
        low = i * rows
        task = await _cancel_after(_bulk_write(conn, method, low, rows), rng.randrange(40))
        written = await _count(conn, low, low + rows)
        if task.cancelled():
            assert written in {0, rows}
            outcomes.add(f"cancelled, {written} written")
        else:
            assert task.result() == rows
            assert written == rows
            outcomes.add("finished")
        await _assert_consistent(conn, i)
    assert "cancelled, 0 written" in outcomes
    assert loop_errors.messages() == []


@pytest.mark.usefixtures("table")
@pytest.mark.parametrize("method", ["execute_many", "insert_many", "copy_in"])
async def test_bulk_write_cancelled_inside_a_transaction_leaves_it_usable(
    conn: fastpg.Connection,
    rng: random.Random,
    method: str,
) -> None:
    rows = 3000
    expected = 0
    async with conn.transaction() as tx:
        for i in range(25):
            low = i * rows
            task = await _cancel_after(_bulk_write(tx, method, low, rows), rng.randrange(40))
            # Its own savepoint was rolled back (or released): the transaction isn't aborted.
            (written,) = await tx.select_one(
                "SELECT count(*)::int FROM edge_cancel WHERE id >= $1 AND id < $2",
                low,
                low + rows,
            )
            assert written in ({0, rows} if task.cancelled() else {rows})
            expected += written
    assert await _count(conn, 0, 25 * rows) == expected


@pytest.mark.usefixtures("table")
@pytest.mark.parametrize("method", ["execute_many", "insert_many", "copy_in"])
async def test_pool_bulk_write_cancelled_at_random_points(
    dsn: str,
    conn: fastpg.Connection,
    rng: random.Random,
    loop_errors: LoopErrors,
    method: str,
) -> None:
    rows = 3000
    async with await fastpg.create_pool(dsn, min_size=1, max_size=1) as pool:
        for i in range(25):
            low = i * rows
            task = await _cancel_after(_bulk_write(pool, method, low, rows), rng.randrange(40))
            written = await _count(conn, low, low + rows)
            assert written in ({0, rows} if task.cancelled() else {rows})
        stats = await _settled(pool)
        assert (stats.size, stats.acquired) == (1, 0)
        async with pool.acquire() as acquired:
            assert not await _in_transaction(acquired)
    assert loop_errors.messages() == []


@pytest.mark.usefixtures("table")
async def test_transaction_cancelled_at_random_points(
    conn: fastpg.Connection,
    rng: random.Random,
    loop_errors: LoopErrors,
) -> None:
    for i in range(120):
        task = await _cancel_after(_transaction_insert(conn, i), rng.randrange(8))
        if not task.cancelled():
            task.result()
            assert await _count(conn, i, i + 1) == 1
        await _assert_consistent(conn, i)
        # The connection takes a new transaction: none is left marked active.
        async with conn.transaction() as tx:
            assert await tx.select_one("SELECT $1::int", i) == (i,)
    assert loop_errors.messages() == []


@pytest.mark.usefixtures("table")
async def test_savepoint_cancelled_at_random_points(
    conn: fastpg.Connection,
    rng: random.Random,
    loop_errors: LoopErrors,
) -> None:
    async with conn.transaction() as tx:
        for i in range(80):
            task = await _cancel_after(_savepoint_insert(tx, i), rng.randrange(6))
            if not task.cancelled():
                task.result()
            # The transaction is still usable (not aborted) and a new savepoint works.
            assert await tx.select_one("SELECT $1::int", i) == (i,)
            async with tx.savepoint():
                await tx.execute("INSERT INTO edge_cancel VALUES ($1, 'after')", 1000 + i)
    assert await _count(conn, 1000, 1080) == 80
    await _assert_consistent(conn, 0)
    assert loop_errors.messages() == []


@pytest.mark.parametrize("deadline", [0.0, 0.0005, 0.002, 0.01])
async def test_asyncio_timeout_around_queries(
    conn: fastpg.Connection,
    loop_errors: LoopErrors,
    deadline: float,
) -> None:
    for i in range(20):
        try:
            async with asyncio.timeout(deadline):
                assert await conn.select_one("SELECT $1::int, pg_sleep(0.001)", i) == (i, None)
        except TimeoutError:
            pass
        await _assert_consistent(conn, i)
    assert loop_errors.messages() == []


# --- connecting -----------------------------------------------------------------------------


async def test_connect_cancelled_at_random_points_leaks_nothing(
    conn: fastpg.Connection,
    dsn: str,
    rng: random.Random,
    loop_errors: LoopErrors,
) -> None:
    tagged = f"{dsn}?application_name=edge_cancel_connect"
    fds = _open_fds()
    for _ in range(40):
        await _connect_cancelled_after(tagged, rng.randrange(12))
    query = "SELECT count(*) FROM pg_stat_activity WHERE application_name = 'edge_cancel_connect'"
    async with asyncio.timeout(5):
        while (await conn.select_one(query))[0] or _open_fds() > fds:
            await asyncio.sleep(0.01)
    assert loop_errors.messages() == []


@pytest.mark.parametrize("sslmode", ["disable", "require"])
async def test_connect_timeout_against_a_silent_server_closes_the_socket(
    silent_server: socket.socket,
    sslmode: str,
) -> None:
    # No connect timeout is built in: asyncio.timeout() is the way to bound a connect. The
    # server never answers the startup (or TLS) request; once the connect is cancelled, the
    # client must close its socket rather than keep the handshake waiting forever.
    port = silent_server.getsockname()[1]
    fds = _open_fds()
    dsn = f"postgresql://u:p@127.0.0.1:{port}/db?sslmode={sslmode}"
    assert await asyncio.create_task(_connect_times_out(dsn, 0.2))
    accepted, _ = silent_server.accept()
    with accepted:
        accepted.settimeout(5)
        assert accepted.recv(1024)  # the startup or SSLRequest packet
        assert accepted.recv(1024) == b""  # then EOF: the client closed its socket
    assert _open_fds() == fds


@pytest.mark.timeout(60)
async def test_tls_connect_cancelled_at_random_points_leaks_nothing(
    tls_server: tuple[str, Any],
    rng: random.Random,
    loop_errors: LoopErrors,
) -> None:
    dsn, ca = tls_server
    verified = f"{dsn}?sslmode=verify-full&sslrootcert={ca}&application_name=edge_tls_cancel"
    fds = _open_fds()
    for _ in range(30):
        await _connect_cancelled_after(verified, rng.randrange(30))
    await _wait_until_alone(verified, "edge_tls_cancel")
    async with asyncio.timeout(5):
        while _open_fds() > fds:
            await asyncio.sleep(0.01)
    assert loop_errors.messages() == []


# --- pools ----------------------------------------------------------------------------------


@pytest.mark.timeout(60)
async def test_pool_shared_queries_cancelled_at_random_points(
    dsn: str,
    rng: random.Random,
    loop_errors: LoopErrors,
) -> None:
    pool = await fastpg.create_pool(dsn, min_size=1, max_size=3, max_inflight_per_conn=2)
    async with pool:
        for _ in range(10):
            tasks = [asyncio.create_task(_pool_query(pool, i)) for i in range(60)]
            for task in rng.sample(tasks, 25):
                for _ in range(rng.randrange(4)):
                    await asyncio.sleep(0)
                task.cancel()
            await asyncio.wait(tasks)
            for i, task in enumerate(tasks):
                _check_pool_query(task, i)
        stats = await _settled(pool)
        assert (stats.in_flight, stats.waiting, stats.waiting_acquire, stats.acquired) == (0,) * 4
        assert stats.idle == stats.size
        rows = await asyncio.gather(*(pool.select_one("SELECT $1::int", i) for i in range(50)))
        assert rows == [(i,) for i in range(50)]
    assert loop_errors.messages() == []


@pytest.mark.usefixtures("table")
@pytest.mark.timeout(60)
async def test_pool_checkouts_cancelled_while_waiting_and_inside(
    dsn: str,
    conn: fastpg.Connection,
    rng: random.Random,
    loop_errors: LoopErrors,
) -> None:
    async with await fastpg.create_pool(dsn, min_size=1, max_size=2) as pool:
        for round_ in range(15):
            base = round_ * 100
            tasks = [
                asyncio.create_task(
                    _pool_transaction(pool, base + i) if i % 2 else _pool_acquire(pool, base + i),
                )
                for i in range(20)
            ]
            for task in rng.sample(tasks, 10):
                for _ in range(rng.randrange(10)):
                    await asyncio.sleep(0)
                task.cancel()
            await asyncio.wait(tasks)
            for i, task in enumerate(tasks):
                if task.cancelled():
                    continue
                if i % 2:
                    task.result()
                    assert await _count(conn, base + i, base + i + 1) == 1
                else:
                    assert task.result() == (base + i,)
        stats = await _settled(pool)
        assert (stats.waiting_acquire, stats.acquired, stats.in_flight) == (0, 0, 0)
        # Every connection went back clean: no session setting survived its checkout.
        async with asyncio.timeout(5):
            names = await asyncio.gather(
                *(pool.select_one("SELECT current_setting('application_name')") for _ in range(8)),
            )
        assert ("edge-cancel",) not in names
    assert loop_errors.messages() == []
