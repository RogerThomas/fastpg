"""Event loop lifecycle, threads, fork() and garbage collection.

Some of these tests run their own event loops, so they are plain (sync) tests: the session's
loop isn't running while they do.
"""

import asyncio
import gc
import os
import resource
import sys
import sysconfig
import threading
import warnings
from collections.abc import Callable, Coroutine
from functools import partial
from pathlib import Path
from typing import Any

import pytest

import fastpg


def _open_fds() -> int:
    gc.collect()
    return len(list(Path("/dev/fd").iterdir()))


def _tagged(dsn: str, name: str) -> str:
    return f"{dsn}?application_name={name}"


async def _backends(dsn: str, name: str) -> int:
    async with await fastpg.connect(dsn) as conn:
        query = "SELECT count(*)::int FROM pg_stat_activity WHERE application_name = $1"
        (count,) = await conn.select_one(query, name)
        return count


async def _wait_for_backends(conn: fastpg.Connection, name: str, count: int) -> None:
    query = "SELECT count(*)::int FROM pg_stat_activity WHERE application_name = $1"
    async with asyncio.timeout(5):
        while (await conn.select_one(query, name))[0] != count:
            await asyncio.sleep(0.01)


async def _wait_for_fds(count: int) -> None:
    async with asyncio.timeout(5):
        while _open_fds() > count:
            await asyncio.sleep(0.01)


async def _select(conn: fastpg.Connection, value: int) -> tuple[Any, ...]:
    async with asyncio.timeout(2):
        return await conn.select_one("SELECT $1::int", value)


async def _open_and_leave_open(dsn: str, holder: list[object]) -> None:
    conn = await fastpg.connect(dsn)
    await conn.select_one("SELECT 1")
    holder.append(conn)
    holder.append(await fastpg.create_pool(dsn, min_size=2, max_size=2))


async def _leave_a_query_in_flight(dsn: str, holder: list[object]) -> None:
    """Open a connection and leave a query running on it (asyncio.run then cancels its task
    when this returns)."""
    conn = await fastpg.connect(dsn)
    holder.append(asyncio.create_task(conn.select_one("SELECT pg_sleep(0.2)")))
    holder.append(conn)
    await asyncio.sleep(0.01)


async def _leave_a_shared_query_in_flight(dsn: str, holder: list[object]) -> None:
    """The same with a pool's shared query (cancelling it spawns the pool's ghost flush)."""
    pool = await fastpg.create_pool(dsn, min_size=1, max_size=1)
    holder.append(asyncio.create_task(pool.select_one("SELECT pg_sleep(0.2)")))
    holder.append(pool)
    await asyncio.sleep(0.01)


async def _wait_for_no_backends(dsn: str, name: str) -> None:
    async with asyncio.timeout(5):
        while await _backends(dsn, name):
            await asyncio.sleep(0.05)


async def _open_and_drop(dsn: str) -> None:
    conn = await fastpg.connect(dsn)
    await conn.select_one("SELECT 1")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ResourceWarning)
        del conn  # unclosed: refcounting runs __del__ right here


async def _drop_pool(dsn: str, name: str) -> int:
    """Open a pool, drop it unclosed; how many sessions it still has a moment later."""
    pool = await fastpg.create_pool(_tagged(dsn, name), min_size=2, max_size=2)
    del pool
    gc.collect()
    await asyncio.sleep(0.05)
    return await _backends(dsn, name)


async def _connect_into(dsn: str, holder: list[fastpg.Connection]) -> None:
    holder.append(await fastpg.connect(dsn))


def _run_and_collect[T](coro: Coroutine[Any, Any, T]) -> T:
    """``asyncio.run(coro)``, then a collection (of what the closed loop left behind)."""
    result = asyncio.run(coro)
    gc.collect()
    return result


def _drop_all(holder: list[object]) -> None:
    holder.clear()
    gc.collect()


def _run_in_thread[T](call: Callable[[], T]) -> T | BaseException:
    """``call()`` on a new thread: its result, or what it raised."""
    outcome: list[T | BaseException] = []
    thread = threading.Thread(target=_record, args=(call, outcome))
    thread.start()
    thread.join()
    return outcome[0]


def _record[T](call: Callable[[], T], outcome: list[T | BaseException]) -> None:
    try:
        outcome.append(call())
    except BaseException as e:  # noqa: BLE001 - handed to the test thread
        outcome.append(e)


def _run_coroutine_on[T](loop: asyncio.AbstractEventLoop, coro: Coroutine[Any, Any, T]) -> T:
    return loop.run_until_complete(coro)


def _start(coro: Coroutine[Any, Any, Any]) -> BaseException | None:
    """Run ``coro`` up to its first suspension, outside any loop: what it raised, if anything."""
    try:
        coro.send(None)
    except StopIteration:
        return None
    except BaseException as e:  # noqa: BLE001 - reported to the parent process
        return e
    coro.close()
    return None


def _in_child(action: Callable[[], object]) -> None:
    """Run ``action`` in a fork()ed child, then leave at once (no cleanup of the parent's
    objects beyond what ``action`` does). Exit status 0 if it returned True."""
    try:
        status = 0 if action() is True else 1
    except BaseException:  # noqa: BLE001 - nothing may escape into pytest in the child
        status = 2
    os._exit(status)


def _fork(action: Callable[[], object]) -> int:
    """Fork; the child runs ``action`` (see ``_in_child``). Returns the child's exit status."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)  # "multi-threaded, use of fork()"
        child = os.fork()
    if child == 0:
        _in_child(action)
    _, status = os.waitpid(child, 0)
    return os.waitstatus_to_exitcode(status)


class _DropInChild:
    """Drops the child's last reference to a connection (running its ``__del__`` there)."""

    def __init__(self, holder: list[fastpg.Connection]) -> None:
        self._holder = holder

    def __call__(self) -> bool:
        self._holder.clear()
        gc.collect()
        return True


class _QueryInChild:
    """Tries a query on an inherited connection in the child: it must be refused."""

    def __init__(self, conn: fastpg.Connection) -> None:
        self._conn = conn

    def __call__(self) -> bool:
        error = _start(self._conn.select_one("SELECT 'child'"))
        return isinstance(error, fastpg.InterfaceError) and "another process" in str(error)


class _CloseInChild:
    """Closes an inherited connection in the child: it must return at once, sending nothing."""

    def __init__(self, conn: fastpg.Connection) -> None:
        self._conn = conn

    def __call__(self) -> bool:
        return _start(self._conn.close()) is None and self._conn.closed


class _TerminateInChild:
    """Terminates an inherited connection in the child: it must send nothing either."""

    def __init__(self, conn: fastpg.Connection) -> None:
        self._conn = conn

    def __call__(self) -> bool:
        self._conn.terminate()
        return self._conn.closed


# --- the loop goes away ---------------------------------------------------------------------


def test_connections_left_open_when_the_loop_closes_release_everything(dsn: str) -> None:
    # asyncio.run() ends without closing them: once collected (with a ResourceWarning), the
    # sockets and the native objects' descriptors are released and the sessions end.
    name = "edge_loop_closed"
    fds = _open_fds()
    holder: list[object] = []
    asyncio.run(_open_and_leave_open(_tagged(dsn, name), holder))
    with pytest.warns(ResourceWarning, match="unclosed fastpg connection"):
        _drop_all(holder)
    assert _open_fds() == fds
    assert asyncio.run(_backends(dsn, name)) == 0


def test_a_query_in_flight_when_the_loop_closes(dsn: str) -> None:
    name = "edge_loop_closed_busy"
    fds = _open_fds()
    holder: list[object] = []
    asyncio.run(_leave_a_query_in_flight(_tagged(dsn, name), holder))
    with pytest.warns(ResourceWarning, match="unclosed fastpg connection"):
        _drop_all(holder)
    assert _open_fds() == fds
    asyncio.run(_wait_for_no_backends(dsn, name))  # once its pg_sleep is over


@pytest.mark.xfail(
    # Free-threaded builds' collector sometimes reclaims the cycle anyway: not strict there.
    strict=not sysconfig.get_config_var("Py_GIL_DISABLED"),
    reason="open: a task pending when the loop closes, awaiting a native future, forms a "
    "reference cycle through the native connection that the GC can't see (notes/"
    "edge-cases-lifecycle.md, 'Uncollectable cycles through a native connection')",
)
def test_a_shared_query_in_flight_when_the_loop_closes(dsn: str) -> None:
    # asyncio.run() cancels the query's task; the pool then spawns its ghost flush (a task
    # awaiting a no-op on the connection), which asyncio.run never sees or cancels.
    name = "edge_loop_closed_shared"
    fds = _open_fds()
    holder: list[object] = []
    asyncio.run(_leave_a_shared_query_in_flight(_tagged(dsn, name), holder))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ResourceWarning)
        _drop_all(holder)
    assert _open_fds() == fds


def test_a_loop_run_on_another_thread_keeps_serving_its_connection(dsn: str) -> None:
    # One loop, but run_until_complete() called from a thread other than the one it ran on
    # when the connection was opened.
    loop = asyncio.new_event_loop()
    try:
        holder: list[fastpg.Connection] = []
        loop.run_until_complete(_connect_into(dsn, holder))
        conn = holder[0]
        assert _run_in_thread(partial(_run_coroutine_on, loop, _select(conn, 5))) == (5,)
        assert loop.run_until_complete(_select(conn, 6)) == (6,)  # and back
        loop.run_until_complete(conn.close())
    finally:
        loop.close()


def test_a_connection_awaited_from_another_loop_raises_and_stays_usable(dsn: str) -> None:
    loop = asyncio.new_event_loop()
    try:
        holder: list[fastpg.Connection] = []
        loop.run_until_complete(_connect_into(dsn, holder))
        conn = holder[0]
        with pytest.raises(RuntimeError, match="attached to a different loop"):
            asyncio.run(_select(conn, 1))
        assert loop.run_until_complete(_select(conn, 2)) == (2,)
        loop.run_until_complete(conn.close())
    finally:
        loop.close()


async def test_a_connection_awaited_from_a_loop_on_another_thread_raises(
    conn: fastpg.Connection,
) -> None:
    outcome = await asyncio.to_thread(_run_in_thread, partial(asyncio.run, _select(conn, 1)))
    assert isinstance(outcome, RuntimeError)
    assert "attached to a different loop" in str(outcome)
    assert [await _select(conn, i) for i in range(3)] == [(0,), (1,), (2,)]


# --- garbage collection ---------------------------------------------------------------------


async def test_unclosed_connection_collected_on_another_thread_is_closed(
    dsn: str,
    conn: fastpg.Connection,
) -> None:
    # The Poker path: __del__ runs on a thread other than the loop's.
    name = "edge_gc_thread"
    fds = _open_fds()
    holder: list[fastpg.Connection] = []
    await _connect_into(_tagged(dsn, name), holder)
    await _wait_for_backends(conn, name, 1)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ResourceWarning)
        await asyncio.to_thread(holder.clear)
    await _wait_for_backends(conn, name, 0)
    await _wait_for_fds(fds)


@pytest.mark.timeout(120)
async def test_connections_opened_and_dropped_in_a_loop_leak_nothing(
    dsn: str,
    conn: fastpg.Connection,
) -> None:
    name = "edge_gc_loop"
    tagged = _tagged(dsn, name)
    for _ in range(20):  # warm up: allocator pools, caches
        await _open_and_drop(tagged)
    await _wait_for_backends(conn, name, 0)
    fds = _open_fds()
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    for _ in range(300):
        await _open_and_drop(tagged)
    await _wait_for_backends(conn, name, 0)
    await _wait_for_fds(fds)
    grown = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - rss
    assert grown < (64 << 20 if sys.platform == "darwin" else 64 << 10)  # bytes / KiB


def test_dropping_an_unclosed_pool_keeps_it_running_until_its_loop_ends(dsn: str) -> None:
    # Its upkeep task (owned by the loop) keeps it alive, like an unclosed asyncio.Server:
    # dropping the last reference closes nothing. Once the loop ends, its connections are
    # collected (with a ResourceWarning each) and their sessions end.
    name = "edge_gc_pool"
    fds = _open_fds()
    with pytest.warns(ResourceWarning, match="unclosed fastpg connection"):
        alive = _run_and_collect(_drop_pool(dsn, name))
    assert alive == 2
    assert _open_fds() == fds
    assert asyncio.run(_backends(dsn, name)) == 0


# --- fork() ---------------------------------------------------------------------------------


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
async def test_a_forked_child_dropping_the_connection_leaves_the_parent_alone(
    dsn: str,
) -> None:
    holder: list[fastpg.Connection] = []
    await _connect_into(dsn, holder)  # the only reference: the child's clear() drops it
    assert _fork(_DropInChild(holder)) == 0
    # The child's __del__ must not have closed the parent's session (a Terminate on the
    # socket both processes share).
    assert [await _select(holder[0], i) for i in range(3)] == [(0,), (1,), (2,)]
    await holder[0].close()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
async def test_a_forked_child_using_the_connection_gets_an_error(
    conn: fastpg.Connection,
) -> None:
    assert _fork(_QueryInChild(conn)) == 0
    assert [await _select(conn, i) for i in range(3)] == [(0,), (1,), (2,)]


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
async def test_a_forked_child_closing_the_connection_leaves_the_parent_alone(
    conn: fastpg.Connection,
) -> None:
    assert _fork(_CloseInChild(conn)) == 0
    assert [await _select(conn, i) for i in range(3)] == [(0,), (1,), (2,)]


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
async def test_a_forked_child_terminating_the_connection_leaves_the_parent_alone(
    conn: fastpg.Connection,
) -> None:
    assert _fork(_TerminateInChild(conn)) == 0
    assert [await _select(conn, i) for i in range(3)] == [(0,), (1,), (2,)]
