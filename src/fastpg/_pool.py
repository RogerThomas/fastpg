"""Connection pool: routed, pipelined shared queries plus exclusive checkouts.

Two ways to use a pool:

* **Shared mode**: ``await pool.select_one(...)`` and friends. Each statement runs on its
  own (autocommit) and is routed to a connection, where it may be pipelined behind other
  shared statements. Nothing is checked out.
* **Exclusive mode**: ``async with pool.acquire() as conn`` / ``pool.transaction()``. The
  connection leaves shared rotation until the block exits, then its session state is reset.

Routing a shared statement (see ``notes/pool.md`` for the reasoning and measurements):

1. a shared connection with nothing in flight takes it (the one most recently idle, picked
   in constant time: the pool keeps its idle connections in a stack);
2. otherwise, while below ``max_size``, a new connection starts opening in the background
   (the statement doesn't wait for it);
3. meanwhile the statement is pipelined onto the connection with the lowest stall score,
   among those with fewer than ``max_inflight_per_conn`` statements in flight. The score is
   ``max(expected work queued, age of the oldest statement in flight)``, where a
   statement's expected work is a moving average of its SQL text's past service times. So
   a connection behind a known-slow statement is avoided from the moment it's sent, and
   one stuck behind an unexpectedly slow statement once that has run for a while;
4. if every connection is at the cap, the statement waits (FIFO) for the first free slot.
   It is sent by the code that frees the slot, so a busy connection is refilled at once
   and its queries are still written together (see ``Pool._hand``).

Everything runs on the event loop that created the pool (fastpg connections are bound to
it), including every connection's protocol: there are no locks and no extra threads. The
per-statement bookkeeping (statements in flight, learned service times, scoring) is in Rust,
``fastpg._native.Router``: two or three calls per statement instead of a few hundred
nanoseconds of Python (``notes/perf-pool.md``).
"""

import asyncio
import math
import time
from collections import deque
from contextlib import asynccontextmanager, suppress
from dataclasses import InitVar, dataclass, field
from string.templatelib import Template
from typing import TYPE_CHECKING, Any, ClassVar, Concatenate, Self, overload

import msgspec

from fastpg._connection import (
    Connection,
    Fetcher,
    IsolationLevel,
    Strategy,
    Transaction,
    cancelling,
    connect,
)
from fastpg._group import grouping_for
from fastpg._named import NamedBinder, NamedParams
from fastpg._native import Router
from fastpg._template import TemplateCompiler
from fastpg.errors import ConnectionClosedError, DatabaseError, FastPGError

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine, Iterable, Sequence
    from types import TracebackType

    from fastpg._writes import Table


@dataclass(slots=True, eq=False)
class _Entry:
    """One pooled connection and its routing state."""

    SHARED: ClassVar[int] = 0
    """In shared rotation."""
    DRAINING: ClassVar[int] = 1
    """Takes no new shared statements; reserved for a waiting acquire()."""
    RETIRING: ClassVar[int] = 2
    """Takes no new shared statements; closed once idle (max_lifetime)."""
    EXCLUSIVE: ClassVar[int] = 3
    """Checked out by acquire() / transaction()."""
    GONE: ClassVar[int] = 4
    """Removed from the pool."""

    conn: Connection
    born_ns: int
    # Its id in the pool's Router: the shared statements in flight on it, their expected
    # service times, and when it last became idle.
    slot: int
    fetcher: Fetcher = field(init=False)  # conn's statement paths, for shared statements
    state: int = SHARED
    # Tokens of cancelled statements that may still be running on the server.
    ghosts: list[int] = field(default_factory=list[int])
    flushing: bool = False

    def __post_init__(self) -> None:
        self.fetcher = Fetcher.of(self.conn)


@dataclass(slots=True, eq=False)
class _Waiter:
    """A shared statement waiting (FIFO) for a slot (rule 4), and once sent, its slot.

    ``Pool._hand`` sends it with ``future`` as its native result future, so its caller
    wakes up once, when the result is in.
    """

    future: asyncio.Future[Any]
    kind: int
    query: str
    params: tuple[object, ...]
    into: type[Any] | None
    strategy: Strategy
    # Set by Pool._hand once sent (token 0: not sent). `call` resolves to the result:
    # `future` itself, or a coroutine that decodes it (msgpack / json).
    entry: _Entry = field(init=False)
    call: Awaitable[Any] = field(init=False)
    token: int = field(init=False, default=0)


@dataclass(frozen=True, slots=True)
class PoolStats:
    """A snapshot of a pool's state (``Pool.stats()``)."""

    size: int
    """Open connections, shared and acquired."""
    opening: int
    """Connections being opened."""
    idle: int
    """Shared connections with nothing in flight."""
    acquired: int
    """Connections checked out by ``acquire()`` / ``transaction()``."""
    in_flight: int
    """Shared statements in flight (including cancelled ones still running server-side)."""
    waiting: int
    """Shared statements waiting for a free slot."""
    waiting_acquire: int
    """``acquire()`` / ``transaction()`` calls waiting for an idle connection."""


def _check_seconds(name: str, value: float | None) -> None:
    if value is not None and not 0 < value < math.inf:  # NaN fails both comparisons
        msg = f"{name} must be a positive number of seconds, or None (never), got {value!r}"
        raise ValueError(msg)


def _check_config(
    min_size: int,
    max_size: int,
    max_inflight_per_conn: int,
    statement_cache_size: int,
    max_lifetime: float | None,
    max_idle: float | None,
) -> None:
    """Reject an invalid configuration (before any connection is opened)."""
    if max_size < 1 or not 0 <= min_size <= max_size:
        msg = f"need 0 <= min_size <= max_size and max_size >= 1, got {min_size}, {max_size}"
        raise ValueError(msg)
    if max_inflight_per_conn < 1:
        msg = f"max_inflight_per_conn must be >= 1, got {max_inflight_per_conn}"
        raise ValueError(msg)
    if statement_cache_size < 0:
        msg = f"statement_cache_size must be >= 0, got {statement_cache_size}"
        raise ValueError(msg)
    _check_seconds("max_lifetime", max_lifetime)
    _check_seconds("max_idle", max_idle)


async def _connect_all(dsn: str, count: int, statement_cache_size: int) -> list[Connection]:
    """Open ``count`` connections concurrently; on any failure close the others and raise."""
    loop = asyncio.get_running_loop()
    tasks = [
        loop.create_task(connect(dsn, statement_cache_size=statement_cache_size))
        for _ in range(count)
    ]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for task in tasks:
            if not task.cancelled() and task.exception() is None:
                await task.result().close()
        raise


def _is_fatal(error: BaseException) -> bool:
    """Whether ``error`` means the session is gone (it may not be ``closed`` just yet)."""
    if isinstance(error, ConnectionClosedError):
        return True
    return isinstance(error, DatabaseError) and (
        error.sqlstate.startswith("08") or error.sqlstate in {"57P01", "57P02", "57P03"}
    )


@dataclass(slots=True, eq=False)
class Pool:
    """A pool of connections to one database, bound to the event loop that created it.

    Create with ``await fastpg.create_pool(dsn, ...)``; close with ``await pool.close()`` or
    ``async with await fastpg.create_pool(...) as pool``.

    The configuration fields (``dsn``, ``min_size``, ...) are read-only.

    Shared statements (``select_*``, ``execute``, ``execute_script``) must not change session
    state (``SET``, ``BEGIN``, temp tables, ...): the session is shared with other callers
    and isn't reset. Use ``acquire()`` or ``transaction()`` for that.
    """

    _DEFAULT_COST_NS: ClassVar[int] = 50_000
    """Expected service time of a statement not seen before: roughly one point lookup."""

    _MAX_COSTS: ClassVar[int] = 10_000
    """Bound on the per-SQL cost table (cleared when exceeded, e.g. by generated SQL)."""

    _RESET: ClassVar[str] = (
        "SELECT pg_advisory_unlock_all(); CLOSE ALL; UNLISTEN *; RESET ALL;"
        " RESET SESSION AUTHORIZATION; RESET ROLE; DISCARD TEMP"
    )
    """Session reset on release: everything ``DISCARD ALL`` does except dropping prepared
    statements (fastpg's statement cache) and cached plans."""

    _GUARDED_RESET: ClassVar[str] = (
        # In the simple protocol, an implicit transaction starts with the statement, so
        # now() == statement_timestamp() exactly; in a transaction left open by the caller
        # now() is older, and the cast fails, aborting the script before the reset.
        "SELECT (CASE WHEN now() = statement_timestamp() THEN '0'"
        " ELSE 'fastpg: connection released inside a transaction' END)::int; " + _RESET
    )

    _ROLLBACK_RESET: ClassVar[str] = "ROLLBACK; " + _RESET

    # Shared statement kinds (the first three are Fetcher.fetch's result shapes).
    _ONE: ClassVar[int] = 0
    _ONE_OR_NONE: ClassVar[int] = 1
    _ALL: ClassVar[int] = 2
    _EXECUTE: ClassVar[int] = 3
    _SCRIPT: ClassVar[int] = 4
    # t-strings are compiled once, before routing (which learns per SQL text).
    _templates: ClassVar[TemplateCompiler] = TemplateCompiler(max_size=1024)
    # Named-parameter queries too: the pool routes and learns on the ``$n`` SQL text.
    _named: ClassVar[NamedBinder] = NamedBinder(1024)

    dsn: str
    min_size: int = field(kw_only=True)
    max_size: int = field(kw_only=True)
    max_inflight_per_conn: int = field(kw_only=True)
    statement_cache_size: int = field(kw_only=True, default=256)
    max_lifetime: float | None = field(kw_only=True, default=3600.0)
    max_idle: float | None = field(kw_only=True, default=300.0)
    connections: InitVar[Sequence[Connection]] = field(kw_only=True, default=())
    """Already open connections (on the running loop) that become the pool's own."""

    _loop: asyncio.AbstractEventLoop = field(init=False, repr=False)
    _max_lifetime_ns: int = field(init=False, repr=False)
    _max_idle_ns: int = field(init=False, repr=False)
    _tidy_interval: float = field(init=False, repr=False)
    _entries: list[_Entry] = field(init=False, repr=False, default_factory=list[_Entry])
    # The idle connections, so picking one costs no scan: SHARED ones with nothing in
    # flight, and DRAINING ones with nothing in flight (reserved for acquire()). Both are
    # stacks (insertion-ordered dicts, so any entry can also leave in O(1)): the one most
    # recently idle is reused first. That keeps the busy working set on the same, warm
    # connections, and leaves surplus ones at the bottom untouched, so their idle time
    # ages and _tidy closes them after max_idle. A queue (FIFO) would cycle through every
    # connection and keep them all fresh, so the pool would never shrink.
    # _place keeps membership in step with (state, load) on every transition; a shared
    # statement pops its connection off the stack (popitem: the top) just before _begin.
    _idle: dict[_Entry, None] = field(init=False, repr=False, default_factory=dict[_Entry, None])
    _draining: int = field(init=False, repr=False, default=0)
    """Connections in the DRAINING state."""
    _drained: dict[_Entry, None] = field(
        init=False,
        repr=False,
        default_factory=dict[_Entry, None],
    )
    _opening: set[asyncio.Task[Connection]] = field(
        init=False,
        repr=False,
        default_factory=set[asyncio.Task[Connection]],
    )
    """Connections being opened (cancelled if the pool closes meanwhile)."""
    # Waiting shared statements, first come first served.
    _waiting: deque[_Waiter] = field(init=False, repr=False, default_factory=deque[_Waiter])
    # Shared statements in flight per connection, and the SQL texts' service times.
    _router: Router = field(init=False, repr=False)
    _slots: dict[int, _Entry] = field(init=False, repr=False, default_factory=dict[int, _Entry])
    _acquiring: deque[asyncio.Future[_Entry]] = field(
        init=False,
        repr=False,
        default_factory=deque[asyncio.Future[_Entry]],
    )
    _tasks: set[asyncio.Task[Any]] = field(init=False, repr=False, default_factory=set[Any])
    _closing: asyncio.Task[None] | None = field(init=False, repr=False, default=None)
    _wakeup: asyncio.Future[None] | None = field(init=False, repr=False, default=None)
    _maintainer: asyncio.Task[None] = field(init=False, repr=False)

    def __post_init__(self, connections: Sequence[Connection]) -> None:
        _check_config(
            self.min_size,
            self.max_size,
            self.max_inflight_per_conn,
            self.statement_cache_size,
            self.max_lifetime,
            self.max_idle,
        )
        self._loop = asyncio.get_running_loop()
        self._max_lifetime_ns = int(self.max_lifetime * 1e9) if self.max_lifetime else 0
        self._max_idle_ns = int(self.max_idle * 1e9) if self.max_idle else 0
        limits = [s / 4 for s in (self.max_lifetime, self.max_idle) if s]
        self._tidy_interval = max(0.01, min([10.0, *limits]))
        self._router = Router(self._DEFAULT_COST_NS, self._MAX_COSTS)
        for conn in connections:
            self._add(conn)
        self._maintainer = self._loop.create_task(self._maintain())

    def _call(
        self,
        entry: _Entry,
        future: asyncio.Future[Any],
        kind: int,
        query: str,
        params: tuple[object, ...],
        into: type[Any] | None,
        strategy: Strategy,
    ) -> Awaitable[Any]:
        """Send a shared statement on ``entry``; returns what resolves to its result.

        The result arrives in ``future`` (returned as is, except for a msgpack or json fetch:
        then a coroutine that decodes it). The statement is sent by this call, and can fail
        in it (e.g. an unavailable ``strategy``): call it where its errors finish the slot.
        """
        if kind <= self._ALL:
            return entry.fetcher.fetch(future, query, params, kind, into, strategy)
        if kind == self._EXECUTE:
            entry.fetcher.send_execute(future, query, params)
        else:
            entry.fetcher.send_script(future, query)
        return future

    # --- connections --------------------------------------------------------------------

    def _spawn[T](self, coro: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
        task = self._loop.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        if self._wakeup is not None and not self._wakeup.done():
            self._wakeup.set_result(None)
        return task

    def _place(self, entry: _Entry) -> None:
        """Bring ``entry``'s membership of the idle stacks in line with its state and load.

        Call after every change to either. An entry that newly becomes idle goes on top; one
        that already was keeps its place.
        """
        if entry.state > _Entry.DRAINING or self._router.load(entry.slot):
            self._idle.pop(entry, None)
            self._drained.pop(entry, None)
        elif entry.state == _Entry.SHARED:
            self._drained.pop(entry, None)
            self._idle[entry] = None
        else:
            self._idle.pop(entry, None)
            self._drained[entry] = None

    def _set_state(self, entry: _Entry, state: int) -> None:
        # Count draining connections, so _dispatch can skip _rebalance when there's nothing
        # for it to do (no acquire() waiters and nothing draining).
        self._draining += (state == _Entry.DRAINING) - (entry.state == _Entry.DRAINING)
        entry.state = state
        self._router.set_shared(entry.slot, state == _Entry.SHARED)
        self._place(entry)

    def _load(self, entry: _Entry) -> int:
        return self._router.load(entry.slot)

    def _free_slot(self, entry: _Entry) -> None:
        """Forget a GONE entry's router slot, once nothing is in flight on it."""
        self._router.remove(entry.slot)
        del self._slots[entry.slot]

    def _add(self, conn: Connection) -> None:
        now = time.perf_counter_ns()
        entry = _Entry(conn, now, self._router.add())
        self._slots[entry.slot] = entry
        self._entries.append(entry)
        self._place(entry)

    def _grow(self) -> None:
        """Start opening one more connection, unless at max_size (rule 2)."""
        if self._closing is None and len(self._entries) + len(self._opening) < self.max_size:
            task = self._spawn(connect(self.dsn, statement_cache_size=self.statement_cache_size))
            self._opening.add(task)
            task.add_done_callback(self._opened)

    def _opened(self, task: asyncio.Task[Connection]) -> None:
        self._opening.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            if not self._entries and not self._opening:
                # Nothing else can serve the waiters: fail them rather than hang.
                self._fail_waiters(error)
            return
        conn = task.result()
        if self._closing is not None:
            self._spawn(conn.close())
            return
        self._add(conn)
        self._retire_expired()
        self._dispatch()

    def _discard(self, entry: _Entry) -> None:
        """Remove a connection from the pool and close it (gracefully: in-flight work ends)."""
        if entry.state == _Entry.GONE:
            return
        self._set_state(entry, _Entry.GONE)
        self._entries.remove(entry)
        if not self._router.load(entry.slot):
            self._free_slot(entry)  # else once its last statement finishes
        self._spawn(entry.conn.close())
        if (
            len(self._entries) + len(self._opening) < self.min_size
            or self._waiting
            or self._acquiring
        ):
            self._grow()

    def _expired(self, entry: _Entry) -> bool:
        return bool(self._max_lifetime_ns) and (
            time.perf_counter_ns() - entry.born_ns > self._max_lifetime_ns
        )

    def _retire(self, entry: _Entry) -> None:
        """Close an idle ``entry`` that outlived max_lifetime, replacing it first if closing
        it would leave fewer than min_size connections open (make before break).

        It then stays in rotation, healthy, until a new connection is in and ``_opened``
        retires it, so the pool never drops below min_size for a connect's duration. At
        max_size there's no room to open the replacement first: it's closed, then replaced.
        """
        if len(self._entries) > self.min_size or self._closing is not None:
            self._discard(entry)
            return
        if not self._opening:  # else a connection on its way in replaces it
            if len(self._entries) >= self.max_size:
                self._discard(entry)
                return
            self._grow()
        if entry.state in {_Entry.RETIRING, _Entry.EXCLUSIVE}:  # a DRAINING one stays reserved
            self._set_state(entry, _Entry.SHARED)
            self._router.touch(entry.slot)  # idle from now

    def _retire_expired(self) -> None:
        """Retire the idle connections past max_lifetime (one just opened may replace them)."""
        if self._max_lifetime_ns:
            for entry in [e for e in self._idle if self._expired(e)]:
                self._retire(entry)

    def _tidy(self) -> None:
        """Periodic upkeep: retire old and surplus idle connections, refill to min_size."""
        for entry in list(self._entries):
            if entry.state not in {_Entry.SHARED, _Entry.DRAINING} or self._load(entry):
                if entry.state == _Entry.SHARED and self._expired(entry):
                    self._set_state(entry, _Entry.RETIRING)  # closed by _finish once idle
                continue
            if entry.conn.closed or (
                self._max_idle_ns
                and len(self._entries) > self.min_size
                and self._router.idle_ns(entry.slot) > self._max_idle_ns
            ):
                self._discard(entry)
            elif self._expired(entry):
                self._retire(entry)
        if len(self._entries) + len(self._opening) < self.min_size:
            self._grow()

    async def _maintain(self) -> None:
        while True:
            await asyncio.sleep(self._tidy_interval)
            self._tidy()

    # --- routing ------------------------------------------------------------------------

    def _begin(self, entry: _Entry, query: str) -> int:
        """Reserve a slot on ``entry`` (a SHARED one, off the idle stack) for ``query``."""
        return self._router.begin(entry.slot, query)

    def _best_slot(self) -> _Entry | None:
        """An idle shared connection, else the least stalled one under the cap (rule 3).

        An idle one is taken off the idle stack: begin a statement on it right away.
        """
        idle = self._idle
        while idle:
            entry = idle.popitem()[0]
            if not entry.conn.closed:
                return entry
            self._discard(entry)  # died since its last statement
        # Nothing idle (so every shared connection has statements in flight): score them.
        # Score = max(expected work queued, age of the oldest statement): see Router.best.
        slot = self._router.best(self.max_inflight_per_conn)
        best = None if slot is None else self._slots[slot]
        if best is not None and best.conn.closed:  # died since its last statement
            self._discard(best)
            return self._best_slot()
        return best

    def _route(self) -> _Entry | None:
        """Pick a connection for a new shared statement, or None if it must wait."""
        if self._waiting or self._closing is not None:  # FIFO: don't overtake waiters
            return None
        entry = self._best_slot()
        if entry is not None and self._load(entry):
            self._grow()  # if None, the statement waits and its dispatch grows the pool
        return entry

    def _idle_entry(self) -> _Entry | None:
        """An idle connection for acquire(), preferring one drained for it."""
        for stack in (self._drained, self._idle):
            while stack:
                entry = next(reversed(stack))
                if not entry.conn.closed:
                    return entry
                self._discard(entry)
        return None

    def _rebalance(self) -> None:
        """Drain as many busy connections as acquire() waiters can't get otherwise."""
        need = len(self._acquiring) - len(self._opening)
        draining = [e for e in self._entries if e.state == _Entry.DRAINING]
        if need > len(draining):
            shared = [e for e in self._entries if e.state == _Entry.SHARED]
            for _ in range(min(need - len(draining), len(shared))):
                entry = min(shared, key=self._load)
                self._set_state(entry, _Entry.DRAINING)
                shared.remove(entry)
        else:
            for entry in draining[max(need, 0) :]:
                self._set_state(entry, _Entry.SHARED)

    def _settle(
        self,
        entry: _Entry,
        token: int,
        error: BaseException | None = None,
        query: str | None = None,
    ) -> None:
        """Release a shared slot; ``query`` (on success) feeds the service time estimates."""
        left = self._router.finish(entry.slot, token, query)
        if error is None and not entry.state:  # the common case: success, SHARED connection
            if not left:
                self._idle[entry] = None  # idle now: on top of the stack (what _place does)
        elif entry.state == _Entry.GONE:
            if not left:
                self._free_slot(entry)  # deferred by _discard while this was in flight
        elif error is not None and (_is_fatal(error) or entry.conn.closed):
            self._discard(entry)
        elif entry.state == _Entry.RETIRING and not left:
            self._retire(entry)
        elif not left:
            self._place(entry)  # idle now: on top of its stack

    def _hand(self, entry: _Entry, waiter: _Waiter) -> None:
        """Send a waiting statement on ``entry`` (picked by ``_best_slot``).

        Sent right here, by whatever freed the slot, rather than by the waiting task once
        the loop has woken it: that's a loop iteration later, so the connection would run
        below its cap meanwhile, and the woken waiters' queries would each be written with
        a ``send`` of their own (after the flush that coalesces a batch of results' next
        queries has already run). That made queueing cost ~40% of the throughput
        (``notes/pool.md``).
        """
        try:
            token = self._begin(entry, waiter.query)
        except TypeError as e:  # not a str: fail its caller, not whoever freed the slot
            self._place(entry)  # back on the idle stack if _best_slot took it off
            waiter.future.set_exception(e)
            return
        try:
            call = self._call(
                entry,
                waiter.future,
                waiter.kind,
                waiter.query,
                waiter.params,
                waiter.into,
                waiter.strategy,
            )
        except Exception as e:  # noqa: BLE001 - raised by the waiting caller instead
            self._settle(entry, token, e)
            waiter.future.set_exception(e)
            return
        waiter.entry = entry
        waiter.call = call
        waiter.token = token

    def _dispatch(self) -> None:
        """Hand freed capacity to waiters: slots to shared statements, idle connections to
        acquire().

        Shared statements go first: a connection released by acquire() would otherwise go
        straight to the next acquire() waiter, and callers that keep re-acquiring would
        starve shared statements forever. An acquire() waiter isn't starved in turn: the
        connection is drained for it (``_rebalance``) once its statements are sent, and
        those only ever wait for one round of them.
        """
        waiting = self._waiting
        while waiting:
            waiter = waiting[0]
            if waiter.future.done():  # its caller was cancelled
                waiting.popleft()
                continue
            entry = self._best_slot()
            if entry is None:
                break
            waiting.popleft()
            self._hand(entry, waiter)
        acquiring = self._acquiring
        while acquiring:
            entry = self._idle_entry()
            if entry is None:
                break
            future = acquiring.popleft()
            if not future.done():
                self._set_state(entry, _Entry.EXCLUSIVE)
                future.set_result(entry)
        if acquiring or waiting:
            self._grow()
        if acquiring or self._draining:
            self._rebalance()

    def _fail_waiters(self, error: BaseException) -> None:
        for future in (*(w.future for w in self._waiting), *self._acquiring):
            if not future.done():
                future.set_exception(error)
        self._waiting.clear()
        self._acquiring.clear()

    def _finish(
        self,
        entry: _Entry,
        token: int,
        error: BaseException | None = None,
        query: str | None = None,
    ) -> None:
        """Release a shared slot (``_settle``), then hand freed capacity to waiters."""
        self._settle(entry, token, error, query)
        if self._waiting or self._acquiring:
            self._dispatch()

    async def _flush_ghosts(self, entry: _Entry) -> None:
        """Free the slots of cancelled statements once the server is past them.

        A cancelled statement may still run on the server (to its end, unless it was
        cancelled there), so its slot (and the connection's stall score) stays taken until a
        no-op sent after it completes. The no-op is only sent once a server-side cancel is
        over, so the slot is also held until then.
        """
        while entry.ghosts:
            ghosts = entry.ghosts
            entry.ghosts = []
            error: FastPGError | None = None
            try:
                await entry.conn.execute_script("SELECT 1")
            except FastPGError as e:
                error = e
            for token in ghosts:
                self._finish(entry, token, error)
        entry.flushing = False

    def _abandon(self, entry: _Entry, token: int, future: asyncio.Future[Any]) -> None:
        """The caller of a sent shared statement (``future``) gave up: cancel it on the server
        if it is alone on its connection, and keep its slot as a ghost."""
        entry.fetcher.abandon(future)
        entry.ghosts.append(token)
        if not entry.flushing:
            entry.flushing = True
            self._spawn(self._flush_ghosts(entry))

    def _cancel_waiter(self, waiter: _Waiter) -> None:
        """The caller of a waiting statement was cancelled: drop it, or release its slot."""
        future = waiter.future
        if waiter.token and asyncio.iscoroutine(waiter.call):
            waiter.call.close()  # the decoding step, never started
        if future.cancelled():
            if waiter.token:  # sent: the server may still run it
                self._abandon(waiter.entry, waiter.token, future)
            else:  # still queued
                with suppress(ValueError):
                    self._waiting.remove(waiter)
            return
        error = future.exception()  # done just before the cancel landed (and now retrieved)
        if waiter.token:
            self._finish(waiter.entry, waiter.token, error)

    async def _wait(self, waiter: _Waiter) -> Any:  # noqa: ANN401 - typed by the public overloads
        """Queue a statement (FIFO) until ``_dispatch`` sends it, then return its result."""
        if self._closing is not None:
            msg = "pool is closed"
            raise ConnectionClosedError(msg)
        self._waiting.append(waiter)
        self._dispatch()
        future = waiter.future
        try:
            result = await future
            if waiter.call is not future:  # msgpack / json: decode what arrived
                result = await waiter.call
        except asyncio.CancelledError:
            self._cancel_waiter(waiter)
            raise
        except BaseException as e:
            if waiter.token:  # else it failed unsent (in _hand, or the pool closed)
                self._finish(waiter.entry, waiter.token, e)
            raise
        self._finish(waiter.entry, waiter.token, None, waiter.query)
        return result

    async def _query(
        self,
        kind: int,
        query: str | Template,
        params: tuple[object, ...],
        into: type[Any] | None,
        strategy: Strategy,
    ) -> Any:  # noqa: ANN401 - typed by the public overloads
        if isinstance(query, Template):
            # Compile once here: routing learns per SQL text, and the connection gets plain SQL.
            query, params = self._templates.bind(query, params)
        future: asyncio.Future[Any] = self._loop.create_future()
        idle = self._idle
        if idle and not self._waiting and self._closing is None:
            # Rule 1, the common case, without the general routing: the most recently idle
            # connection, taken off the stack. A dead one is dropped, and routing starts over.
            entry = idle.popitem()[0]
            if entry.conn.closed:
                self._discard(entry)
                entry = self._route()
        else:
            entry = self._route()
        if entry is None:
            return await self._wait(_Waiter(future, kind, query, params, into, strategy))
        try:
            token = self._begin(entry, query)
        except TypeError:  # not a str
            self._place(entry)  # back on the idle stack it was just taken off
            raise
        try:
            result = await self._call(entry, future, kind, query, params, into, strategy)
        except asyncio.CancelledError:
            self._abandon(entry, token, future)
            raise
        except BaseException as e:
            self._finish(entry, token, e)
            raise
        self._finish(entry, token, None, query)
        return result

    # --- exclusive checkouts ------------------------------------------------------------

    async def _bulk[**P](
        self,
        write: Callable[Concatenate[Connection, P], Coroutine[Any, Any, int]],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> int:
        """Run a Connection bulk-write method on an exclusively checked-out connection."""
        entry = await self._take()
        try:
            written = await write(entry.conn, *args, **kwargs)
        finally:
            # Bulk writes leave no session state (their transaction/savepoint is closed).
            await self._release(entry, reset=False)
        return written

    async def _take(self) -> _Entry:
        """Check out an idle connection, waiting (FIFO) for one if needed."""
        if self._closing is not None:
            msg = "pool is closed"
            raise ConnectionClosedError(msg)
        if not self._acquiring:
            entry = self._idle_entry()
            if entry is not None:
                self._set_state(entry, _Entry.EXCLUSIVE)
                return entry
        future: asyncio.Future[_Entry] = self._loop.create_future()
        self._acquiring.append(future)
        self._dispatch()
        try:
            return await future
        except asyncio.CancelledError:
            if future.cancelled():
                with suppress(ValueError):
                    self._acquiring.remove(future)
            elif future.exception() is None:  # got one just before the cancel landed
                self._set_state(future.result(), _Entry.SHARED)  # unused, so no reset needed
            self._dispatch()
            raise

    async def _reset(self, conn: Connection) -> bool:
        """Reset session state in one round trip; False if the session can't be trusted."""
        with suppress(FastPGError):
            await conn.execute_script(self._GUARDED_RESET)
            return True
        with suppress(FastPGError):  # left inside a (possibly failed) transaction
            await conn.execute_script(self._ROLLBACK_RESET)
            return True
        return False

    async def _release(self, entry: _Entry, *, reset: bool) -> None:
        try:
            clean = not reset or await self._reset(entry.conn)
        except BaseException:  # cancelled mid-reset: the session state is unknown
            self._discard(entry)
            raise
        if not clean or entry.conn.closed or self._closing is not None:
            self._discard(entry)
        elif self._expired(entry):
            self._retire(entry)
        else:
            self._set_state(entry, _Entry.SHARED)
            self._router.touch(entry.slot)  # idle from now
        self._dispatch()

    async def _give_back(self, entry: _Entry, *, reset: bool) -> None:
        """``_release`` a checked-out connection, in the background if the caller is being
        cancelled (see `cancelling`): the connection then stays out of rotation until its
        reset is done, and the caller doesn't wait for a server that may not answer."""
        if cancelling():
            self._spawn(self._release(entry, reset=reset))
        else:
            await self._release(entry, reset=reset)

    # --- lifecycle ----------------------------------------------------------------------

    async def _shutdown(self) -> None:
        self._fail_waiters(ConnectionClosedError("pool is closed"))
        for task in list(self._opening):  # unused now; one stalled by the server never ends
            task.cancel()
        self._maintainer.cancel()
        with suppress(asyncio.CancelledError):
            await self._maintainer
        for entry in list(self._entries):
            if entry.state != _Entry.EXCLUSIVE:  # checked-out connections close on release
                self._discard(entry)
        while self._entries or self._tasks:
            if self._tasks:
                await asyncio.wait(list(self._tasks))
            else:
                self._wakeup = self._loop.create_future()
                await self._wakeup

    def _terminate(self) -> None:
        """Close every connection at once, checked-out ones included (a cancelled close)."""
        for task in list(self._tasks):  # connects, and the closes of discarded connections
            task.cancel()  # a cancelled Connection.close() terminates its connection
        for entry in self._entries:
            entry.conn.terminate()

    async def _close(self) -> None:
        if self._closing is None:
            self._closing = self._loop.create_task(self._shutdown())
        try:
            await asyncio.shield(self._closing)
        except asyncio.CancelledError:
            # The shutdown goes on (shielded), with nothing left to wait for on the server;
            # checked-out connections still leave the pool when released.
            self._terminate()
            raise

    # --- public API ---------------------------------------------------------------------

    @property
    def closed(self) -> bool:
        """True once ``close()`` has been called."""
        return self._closing is not None

    def stats(self) -> PoolStats:
        """Current sizes, in-flight statements and waiters."""
        entries = self._entries
        return PoolStats(
            size=len(entries),
            opening=len(self._opening),
            idle=len(self._idle),
            acquired=sum(1 for e in entries if e.state == _Entry.EXCLUSIVE),
            in_flight=sum(self._load(e) for e in entries),
            waiting=len(self._waiting),
            waiting_acquire=len(self._acquiring),
        )

    @overload
    async def select_one(
        self,
        query: str | Template,
        /,
        *args: object,
        params: NamedParams | None = None,
        into: type[dict[Any, Any]],
        strategy: Strategy = "auto",
    ) -> dict[str, Any]: ...
    @overload
    async def select_one[T](
        self,
        query: str | Template,
        /,
        *args: object,
        params: NamedParams | None = None,
        into: type[T],
        strategy: Strategy = "auto",
    ) -> T: ...
    @overload
    async def select_one(
        self,
        query: str | Template,
        /,
        *args: object,
        params: NamedParams | None = None,
    ) -> tuple[Any, ...]: ...
    async def select_one(
        self,
        query: str | Template,
        /,
        *args: object,
        params: NamedParams | None = None,
        into: type[Any] | None = None,
        strategy: Strategy = "auto",
    ) -> object:
        """Shared mode ``Connection.select_one``: exactly one row."""
        if params is not None:
            query, args = self._named.bind(query, args, params)
        return await self._query(self._ONE, query, args, into, strategy)

    @overload
    async def select_one_or_none(
        self,
        query: str | Template,
        /,
        *args: object,
        params: NamedParams | None = None,
        into: type[dict[Any, Any]],
        strategy: Strategy = "auto",
    ) -> dict[str, Any] | None: ...
    @overload
    async def select_one_or_none[T](
        self,
        query: str | Template,
        /,
        *args: object,
        params: NamedParams | None = None,
        into: type[T],
        strategy: Strategy = "auto",
    ) -> T | None: ...
    @overload
    async def select_one_or_none(
        self,
        query: str | Template,
        /,
        *args: object,
        params: NamedParams | None = None,
    ) -> tuple[Any, ...] | None: ...
    async def select_one_or_none(
        self,
        query: str | Template,
        /,
        *args: object,
        params: NamedParams | None = None,
        into: type[Any] | None = None,
        strategy: Strategy = "auto",
    ) -> object:
        """Shared mode ``Connection.select_one_or_none``: the only row, or None."""
        if params is not None:
            query, args = self._named.bind(query, args, params)
        return await self._query(self._ONE_OR_NONE, query, args, into, strategy)

    @overload
    async def select_many(
        self,
        query: str | Template,
        /,
        *args: object,
        params: NamedParams | None = None,
        into: type[dict[Any, Any]],
        strategy: Strategy = "auto",
    ) -> list[dict[str, Any]]: ...
    @overload
    async def select_many[T](
        self,
        query: str | Template,
        /,
        *args: object,
        params: NamedParams | None = None,
        into: type[T],
        strategy: Strategy = "auto",
    ) -> list[T]: ...
    @overload
    async def select_many(
        self,
        query: str | Template,
        /,
        *args: object,
        params: NamedParams | None = None,
    ) -> list[tuple[Any, ...]]: ...
    async def select_many(
        self,
        query: str | Template,
        /,
        *args: object,
        params: NamedParams | None = None,
        into: type[Any] | None = None,
        strategy: Strategy = "auto",
    ) -> object:
        """Shared mode ``Connection.select_many``: every row."""
        if params is not None:
            query, args = self._named.bind(query, args, params)
        return await self._query(self._ALL, query, args, into, strategy)

    async def select_and_group[T: msgspec.Struct](
        self,
        query: str | Template,
        /,
        *args: object,
        params: NamedParams | None = None,
        into: type[T],
        strategy: Strategy = "auto",
    ) -> list[T]:
        """Shared mode ``Connection.select_and_group``: the rows grouped into `into` Structs."""
        grouping = grouping_for(into)
        rows = await self.select_many(
            query,
            *args,
            params=params,
            into=grouping.row,
            strategy=strategy,
        )
        return grouping.group(rows)

    async def execute(
        self,
        query: str | Template,
        /,
        *args: object,
        params: NamedParams | None = None,
    ) -> int:
        """Shared mode ``Connection.execute``: one autocommit statement; returns rows affected."""
        if params is not None:
            query, args = self._named.bind(query, args, params)
        affected: int = await self._query(self._EXECUTE, query, args, None, "auto")
        return affected

    async def execute_script(self, query: str | Template, /) -> None:
        """Shared mode ``Connection.execute_script``: runs as one implicit transaction."""
        if isinstance(query, Template):
            query = self._templates.script(query)
        await self._query(self._SCRIPT, query, (), None, "auto")

    async def execute_many(self, query: str, rows: Iterable[Sequence[object]], /) -> int:
        """``Connection.execute_many`` on a connection checked out exclusively for the call."""
        return await self._bulk(Connection.execute_many, query, rows)

    async def insert_many(
        self,
        table: Table,
        rows: Iterable[object],
        /,
        *,
        columns: Sequence[str] | None = None,
    ) -> int:
        """``Connection.insert_many`` on a connection checked out exclusively for the call."""
        return await self._bulk(Connection.insert_many, table, rows, columns=columns)

    async def copy_in(
        self,
        table: Table,
        rows: Iterable[object],
        /,
        *,
        columns: Sequence[str] | None = None,
    ) -> int:
        """``Connection.copy_in`` on a connection checked out exclusively for the call.

        Bulk writes hold their connection exclusively, so they never delay shared statements.
        """
        return await self._bulk(Connection.copy_in, table, rows, columns=columns)

    @asynccontextmanager
    async def acquire(self) -> AsyncGenerator[Connection]:
        """Check out a connection for exclusive use: ``async with pool.acquire() as conn``.

        Waits (FIFO) for an idle connection; busy ones are drained of shared statements if
        the pool can't grow. On exit the session is reset (settings, role, advisory locks,
        cursors, LISTENs, temp tables; a transaction left open is rolled back; prepared
        statements are kept) and the connection rejoins shared rotation. Don't keep using
        ``conn`` after the block.
        """
        entry = await self._take()
        try:
            yield entry.conn
        finally:
            await self._give_back(entry, reset=True)

    @asynccontextmanager
    async def transaction(
        self,
        *,
        isolation: IsolationLevel | None = None,
        readonly: bool = False,
        deferrable: bool = False,
    ) -> AsyncGenerator[Transaction]:
        """Run a transaction on an exclusive connection: ``async with pool.transaction() as tx``.

        Commits on normal exit, rolls back if the block raises. The session reset runs in
        the COMMIT's round trip (before COMMIT, so a failure means nothing was committed).
        """
        entry = await self._take()
        clean = False
        try:
            async with Transaction(
                entry.conn,
                isolation=isolation,
                readonly=readonly,
                deferrable=deferrable,
                pre_commit=self._RESET,
            ) as tx:
                yield tx
            clean = True
        finally:
            await self._give_back(entry, reset=not clean)

    async def close(self) -> None:
        """Close gracefully: new calls and waiters fail with ConnectionClosedError, shared
        statements in flight complete, and checked-out connections close when released.

        If the call is cancelled (e.g. by ``asyncio.timeout``), every connection still open,
        checked-out ones included, is closed at once (`Connection.terminate()`), and the
        cancellation is raised.
        """
        await self._close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._close()


async def create_pool(
    dsn: str,
    *,
    min_size: int = 1,
    max_size: int = 16,
    max_inflight_per_conn: int = 8,
    statement_cache_size: int = 256,
    max_lifetime: float | None = 3600.0,
    max_idle: float | None = 300.0,
) -> Pool:
    """Open a pool with ``min_size`` connections (it grows on demand up to ``max_size``).

    Args:
        dsn: e.g. ``"postgresql://user:pw@host:5432/db"``.
        min_size: Connections kept open even when idle.
        max_size: Upper bound on connections. Connections only open when every existing one
            is busy, so a higher ceiling costs nothing at low load, and leaves room for the
            occasional slow query not to hold up fast ones.
        max_inflight_per_conn: Shared statements pipelined on one connection at most;
            ``1`` gives one-statement-per-connection pooling like asyncpg's.
        statement_cache_size: Per-connection prepared statement cache (see ``connect``).
        max_lifetime: Seconds after which a connection is replaced once idle (None: never).
            If closing it would leave fewer than ``min_size`` open, its replacement is opened
            first and it serves until then (unless the pool is at ``max_size``).
        max_idle: Seconds after which an idle connection above ``min_size`` is closed.
    """
    # Before connecting: a configuration Pool() rejects would leave the connections open.
    _check_config(
        min_size,
        max_size,
        max_inflight_per_conn,
        statement_cache_size,
        max_lifetime,
        max_idle,
    )
    return Pool(
        dsn,
        min_size=min_size,
        max_size=max_size,
        max_inflight_per_conn=max_inflight_per_conn,
        statement_cache_size=statement_cache_size,
        max_lifetime=max_lifetime,
        max_idle=max_idle,
        connections=await _connect_all(dsn, min_size, statement_cache_size),
    )
