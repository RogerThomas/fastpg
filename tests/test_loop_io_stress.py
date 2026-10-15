"""The event-loop-driven protocol under stress and at the edges: big values, big COPYs, many
connections, cancellation storms, server-side kills, and no extra threads or busy loops."""

import asyncio
import gc
import hashlib
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import pytest

import fastpg
from fastpg import errors

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable, Iterator


@pytest.fixture
async def stress_table(conn: fastpg.Connection) -> AsyncIterator[str]:
    """A fresh ``loop_io_stress (id int, v text)`` table; yields its name."""
    await conn.execute_script(
        "DROP TABLE IF EXISTS loop_io_stress; CREATE TABLE loop_io_stress (id int, v text)",
    )
    yield "loop_io_stress"
    await conn.execute_script("DROP TABLE loop_io_stress")


@pytest.fixture
def numeric_dsn(dsn: str) -> str:
    """``dsn`` with its host name resolved, so connecting needs no resolver thread."""
    host = urlsplit(dsn).hostname
    assert host is not None
    return dsn.replace(f"@{host}", f"@{socket.gethostbyname(host)}", 1)


def _rows(count: int) -> Iterator[tuple[int, str]]:
    return ((i, "v") for i in range(count))


def _live_futures() -> int:
    gc.collect()
    return sum(1 for o in gc.get_objects() if isinstance(o, asyncio.Future))


async def _queries(conn: fastpg.Connection, n: int) -> set[int]:
    """``n`` pipelined queries on ``conn``; returns the backend pids that answered."""
    rows = await asyncio.gather(
        *(conn.select_one("SELECT $1::int, pg_backend_pid()", i) for i in range(n)),
    )
    assert [value for value, _ in rows] == list(range(n))
    return {pid for _, pid in rows}


def _cancel_all(tasks: Iterable[asyncio.Task[object]]) -> None:
    for task in tasks:
        task.cancel()


async def _wait_closed(conn: fastpg.Connection) -> None:
    async with asyncio.timeout(5):
        while not conn.closed:
            await asyncio.sleep(0.01)


# --- big values and big writes ----------------------------------------------------------------


async def test_64_mib_bytea_parameter_roundtrip(conn: fastpg.Connection) -> None:
    # Far beyond the socket buffers: the write hits WouldBlock (the loop's add_writer path)
    # and the response arrives over many reads.
    payload = bytes(range(256)) * (256 * 1024)
    await conn.execute_script("CREATE TEMP TABLE big_bytea (v bytea)")
    inserted, small = await asyncio.gather(
        conn.execute("INSERT INTO big_bytea VALUES ($1)", payload),
        conn.select_one("SELECT 1"),  # pipelined behind the big write
    )
    assert (inserted, small) == (1, (1,))
    assert await conn.select_one("SELECT length(v), md5(v) FROM big_bytea") == (
        64 << 20,
        hashlib.md5(payload, usedforsecurity=False).hexdigest(),
    )
    (got,) = await conn.select_one("SELECT v FROM big_bytea")
    assert got == payload
    await conn.execute_script("DROP TABLE big_bytea")


async def test_copy_in_a_million_rows(conn: fastpg.Connection) -> None:
    await conn.execute_script("CREATE TEMP TABLE million (id int, v text)")
    assert await conn.copy_in("million", _rows(1_000_000)) == 1_000_000
    totals = await conn.select_one("SELECT count(*), sum(id), max(id) FROM million")
    assert totals == (1_000_000, 999_999 * 1_000_000 // 2, 999_999)
    await conn.execute_script("DROP TABLE million")


async def test_200_connections_on_one_loop(dsn: str) -> None:
    conns = await asyncio.gather(*(fastpg.connect(dsn) for _ in range(200)))
    try:
        pids = await asyncio.gather(*(_queries(conn, 10) for conn in conns))
    finally:
        await asyncio.gather(*(conn.close() for conn in conns))
    assert all(len(p) == 1 for p in pids)  # each connection's queries ran on its own session
    assert len(set[int]().union(*pids)) == 200
    assert all(conn.closed for conn in conns)


# --- cancellation -------------------------------------------------------------------------


@pytest.mark.parametrize("delay", [0, 0.005], ids=["before-reply", "mid-replies"])
async def test_cancellation_storm(conn: fastpg.Connection, delay: float) -> None:
    assert await conn.select_one("SELECT $1::int", 0) == (0,)
    baseline = _live_futures()
    tasks = [asyncio.create_task(conn.select_one("SELECT $1::int", i)) for i in range(1000)]
    await asyncio.sleep(delay)  # every task has submitted its query
    _cancel_all(tasks)
    await asyncio.wait(tasks)
    assert all(t.cancelled() or t.result() == (i,) for i, t in enumerate(tasks))
    if not delay:
        assert all(t.cancelled() for t in tasks)
    del tasks
    # Pipelined behind all 1000: once it's back, every cancelled query has been answered.
    assert await conn.select_one("SELECT 2") == (2,)
    assert conn.cached_statements == 2
    assert not conn.closed
    assert _live_futures() <= baseline  # no request (and no Python future) is left behind


async def test_pool_cancellation_storm(dsn: str) -> None:
    async with await fastpg.create_pool(dsn, min_size=1, max_size=2) as pool:
        tasks = [asyncio.create_task(pool.select_one("SELECT $1::int", i)) for i in range(1000)]
        await asyncio.sleep(0)
        _cancel_all(tasks)
        await asyncio.wait(tasks)
        assert all(task.cancelled() for task in tasks)
        async with asyncio.timeout(5):  # cancelled statements free their slots once answered
            while (s := pool.stats()).in_flight or s.waiting or s.opening:
                await asyncio.sleep(0.01)
        stats = pool.stats()
        assert (stats.waiting, stats.waiting_acquire, stats.acquired) == (0, 0, 0)
        assert stats.idle == stats.size <= 2
        assert await pool.select_one("SELECT 1") == (1,)


# --- the server going away ----------------------------------------------------------------


async def test_copy_in_fails_promptly_when_server_closes(
    conn: fastpg.Connection,
    dsn: str,
    stress_table: str,
) -> None:
    victim = await fastpg.connect(dsn)
    (pid,) = await victim.select_one("SELECT pg_backend_pid()")
    copy = asyncio.create_task(victim.copy_in(stress_table, _rows(50_000_000)))
    await asyncio.sleep(0.3)  # well into streaming
    assert not copy.done()
    await conn.select_one("SELECT pg_terminate_backend($1)", pid)
    async with asyncio.timeout(10):  # must fail promptly, never hang
        with pytest.raises((errors.OperationalError, fastpg.ConnectionClosedError)):
            await copy
    await _wait_closed(victim)
    await victim.close()
    assert await conn.select_one(f"SELECT count(*) FROM {stress_table}") == (0,)  # noqa: S608


@pytest.mark.parametrize("in_flight", [False, True], ids=["idle", "in-flight"])
async def test_no_busy_loop_after_server_closes(
    conn: fastpg.Connection,
    dsn: str,
    in_flight: bool,  # noqa: FBT001
) -> None:
    victim = await fastpg.connect(dsn)
    (pid,) = await victim.select_one("SELECT pg_backend_pid()")
    sleeper = asyncio.create_task(victim.select_one("SELECT pg_sleep(30)")) if in_flight else None
    await asyncio.sleep(0.05)
    await conn.select_one("SELECT pg_terminate_backend($1)", pid)
    await _wait_closed(victim)
    # A socket at EOF stays readable: had the loop kept it registered, it would call back
    # on every iteration and burn a core.
    start = time.process_time()
    await asyncio.sleep(1)
    assert time.process_time() - start < 0.1
    if sleeper is not None:
        with pytest.raises((errors.OperationalError, fastpg.ConnectionClosedError)):
            await sleeper
    await victim.close()


# --- threads --------------------------------------------------------------------------------


@pytest.mark.skipif(not Path("/proc/self/task").is_dir(), reason="counts threads in /proc")
@pytest.mark.parametrize("loop", ["asyncio", "uvloop"])
def test_starts_no_threads(numeric_dsn: str, loop: str) -> None:
    """Connecting, querying, bulk writes and closing never start a thread."""
    probe = Path(__file__).with_name("thread_probe.py")
    result = subprocess.run(  # noqa: S603 - our own interpreter and script
        [sys.executable, str(probe), numeric_dsn, loop],
        capture_output=True,
        text=True,
        check=True,
        timeout=25,  # under the 30 s per-test timeout; the probe itself takes ~1 s
    )
    before, peak = map(int, result.stdout.split())
    assert peak == before
