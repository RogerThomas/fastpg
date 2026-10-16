# Edge cases: lifecycle, cancellation and misuse

A bug hunt over what happens around the happy path: cancellation at every await point,
closing while busy, transactions misused, event loops that close, move or fork, garbage
collection, pool stress and plain misuse. Every finding has a test in
`tests/test_edge_lifecycle_*.py`; every fix is its own commit, with its test.

A bug here is any of: a hang, a leaked pool slot, connection, future or task, a "Task was
destroyed but it is pending" or "never retrieved" warning, a `ResourceWarning`, a wrong or
unclear error, a crash, or a connection left in a bad session state.

How the tests look for them:

* **Seeded random cancellation.** `_cancel_after(coro, k)` runs the operation as a task and
  cancels it after `k` loop iterations, `k` drawn from `random.Random(<fixed seed>)`, so a
  failure replays. After each one, `_assert_consistent` runs `SELECT $1::int` with a fresh
  marker (a wrong-statement result shows up as the wrong marker) and a simple-protocol
  guard that fails inside a transaction block (`now() = statement_timestamp()` holds only
  for a statement that started its own transaction).
* **`loop_errors`** (`tests/conftest.py`) installs a recording exception handler on the
  running loop; tests assert it got nothing after a `gc.collect()`, which is where
  "never retrieved" and "destroyed but pending" are reported.
* **Leaks** are counted directly: open descriptors (`/dev/fd`), sessions in
  `pg_stat_activity` (by `application_name`), live `fastpg.Connection` objects, peak RSS,
  and, against a listening socket that never answers, whether the client closed its end.

Two test-writing traps, for whoever extends these: a task that `asyncio.timeout` cancelled
keeps the frames it interrupted referenced while it keeps running (run the timed-out call
in a task of its own before counting descriptors), and `pytest.raises`' `ExceptionInfo`
keeps the traceback's frames alive.

## Summary

| # | scenario | behaviour before | bug? | fixed | test |
|---|---|---|---|---|---|
| 1 | cancel `connect()` at random points | sessions and descriptors released | no | | `cancel::test_connect_cancelled_at_random_points_leaks_nothing` |
| 1 | `asyncio.timeout` around `connect()` to a server that never answers (plain and `sslmode=require`) | socket left open and registered with the loop forever, handshake task pending | **yes** | `70c3849` | `cancel::test_connect_timeout_against_a_silent_server_closes_the_socket` |
| 1 | cancel a TLS connect at random points | released | no | | `cancel::test_tls_connect_cancelled_at_random_points_leaks_nothing` |
| 1 | cancel `select_one` before send / while waiting; failing query cancelled | next query correct, no orphaned futures | no | | `cancel::test_select_one_cancelled_at_random_points` |
| 1 | cancel `select_many` mid-stream (20k rows) | rest of the stream read and dropped undecoded | no | | `cancel::test_select_many_cancelled_mid_stream` |
| 1 | cancel msgpack / json (validated) fetches | fine | no | | `cancel::test_validated_fetch_cancelled_at_random_points` |
| 1 | cancel `execute_many` / `insert_many` / `copy_in` at start, mid-stream, end | all-or-nothing (0 or N rows) | no | | `cancel::test_bulk_write_cancelled_at_random_points_is_all_or_nothing` |
| 1 | same inside a caller's transaction, and through a pool | own savepoint rolled back, transaction usable; pool connection clean | no | | `cancel::test_bulk_write_cancelled_inside_a_transaction_leaves_it_usable`, `cancel::test_pool_bulk_write_cancelled_at_random_points` |
| 1 | cancel `transaction()` enter / body / exit | rolled back or committed, never left open; next transaction starts | no | | `cancel::test_transaction_cancelled_at_random_points` |
| 1 | cancel `savepoint()` | transaction stays usable | no | | `cancel::test_savepoint_cancelled_at_random_points` |
| 1 | `asyncio.timeout` around queries (0 to 10 ms) | fine | no | | `cancel::test_asyncio_timeout_around_queries` |
| 1 | cancel pool shared queries (some waiting FIFO, some decoding) | slots freed, stats back to idle | no | | `cancel::test_pool_shared_queries_cancelled_at_random_points` |
| 1 | cancel `pool.acquire()` / `pool.transaction()` waiting and inside | stats idle, sessions reset | no | | `cancel::test_pool_checkouts_cancelled_while_waiting_and_inside` |
| 2 | `conn.close()` with pipelined queries and a streamed result in flight | they complete | no | | `close::test_close_lets_pipelined_queries_and_a_streamed_result_finish` |
| 2 | `conn.close()` during a bulk write | the write commits, a query queued behind it completes | no | | `close::test_close_during_a_bulk_write_lets_it_commit` |
| 2 | `conn.close()` from another task during a transaction | block fails with `ConnectionClosedError` (+ note: ROLLBACK failed too), nothing committed | no | | `close::test_close_during_a_transaction_rolls_it_back` |
| 2 | `close()` bounded by `asyncio.timeout` / one of several `close()` calls cancelled | the shared `_closed` future got cancelled: the other callers raised `CancelledError`, and dropping the connection later warned "unclosed" | **yes** | `beec020` | `close::test_a_close_that_timed_out_still_counts_as_closed`, `close::test_cancelling_one_close_leaves_the_others_waiting` |
| 2 | close twice, concurrently; every method after close | `ConnectionClosedError("connection is closed")` | no | | `close::test_close_twice_and_concurrently`, `close::test_every_method_fails_clearly_after_close` |
| 2 | `async with conn` / `async with pool` exiting on an exception | closed, exception propagates | no | | `close::test_async_with_closes_when_the_block_raises`, `close::test_pool_async_with_closes_when_the_block_raises` |
| 2 | `pool.close()` with statements in flight, waiters, acquire waiters, a checkout, an open transaction, a bulk write, connections opening; twice; cancelled | in-flight work finishes, waiters fail `ConnectionClosedError("pool is closed")`, close waits for checkouts, shutdown survives a cancelled `close()` | no | | `close::test_pool_close_*` |
| 3 | exception in the block | rolled back | no | | `tx::test_an_exception_in_the_block_rolls_back` |
| 3 | server error inside, then more queries | each fails with `DatabaseError` 25P02 "current transaction is aborted"; exit raises 25P02 with a note | no | | `tx::test_queries_after_a_server_error_inside_fail_with_25p02` |
| 3 | COMMIT fails (deferred unique constraint) | `UniqueViolationError` from the block; connection usable | no | | `tx::test_a_failing_commit_surfaces_and_leaves_the_connection_usable` |
| 3 | nested savepoints, inner ones rolled back | correct | no | | `tx::test_nested_savepoints_with_the_inner_one_rolled_back` |
| 3 | second transaction while one is active; re-entering one | `InterfaceError` | no | | `tx::test_a_second_transaction_while_one_is_active_is_refused`, `tx::test_a_transaction_object_is_entered_once` |
| 3 | queries through a `Transaction` after its block, or never entered | ran silently outside the transaction (autocommit); after `pool.transaction()`, inside another caller's transaction on the same pooled connection | **yes** | `4903cdc` | `tx::test_queries_through_a_*`, `tx::test_a_finished_pool_transaction_cannot_reach_its_connection_again` |
| 3 | `pool.transaction()` raising | rolled back, session reset (settings) on release | no | | `tx::test_pool_transaction_that_raises_leaves_the_session_clean` |
| 3 | other tasks' queries through the connection during a transaction | run inside it, as `Connection.transaction`'s docstring says | no (documented) | | `tx::test_other_tasks_queries_on_the_connection_run_inside_its_transaction` |
| 4 | `asyncio.run()` ends with connections and a pool open | `ResourceWarning`, but 4 descriptors and the native state leaked per connection for good | **yes** | `8ac641d` | `loop::test_connections_left_open_when_the_loop_closes_release_everything` |
| 4 | same, with a connection's query in flight | released (after the fix above) | no | | `loop::test_a_query_in_flight_when_the_loop_closes` |
| 4 | same, with a pool's shared query in flight | connection and descriptors leak | **yes, open** | | `loop::test_a_shared_query_in_flight_when_the_loop_closes` (xfail, strict) |
| 4 | the loop is run from another thread than the one it connected on | every query hung and the loop spun at 100% CPU | **yes** | `f139bf8` | `loop::test_a_loop_run_on_another_thread_keeps_serving_its_connection` |
| 4 | awaited from another loop (same thread, or another thread) | asyncio's `RuntimeError` "attached to a different loop"; the statement still runs; connection fine | partly (open, minor) | | `loop::test_a_connection_awaited_from_*` |
| 4 | `fork()`: the child queries, closes or drops an inherited connection | the query went out on the shared socket and killed (or desynchronised) the parent's connection; dropping it sent Terminate and ended the parent's session | **yes** | `e6a2915` | `loop::test_a_forked_child_*` |
| 5 | unclosed connection dropped on the loop thread | `ResourceWarning`, session closed | no | | `test_connection.py::test_unclosed_connection_warns_and_cleans_up` |
| 5 | `__del__` on another thread (the Poker path) | session stayed open, descriptors leaked until the loop closed | **yes** | `d6f7037` | `loop::test_unclosed_connection_collected_on_another_thread_is_closed` |
| 5 | 300 connections opened and dropped unclosed | no descriptor, session or RSS growth | no | | `loop::test_connections_opened_and_dropped_in_a_loop_leak_nothing` |
| 5 | unclosed pool dropped | stays alive (its upkeep task holds it) until its loop ends, no warning of its own | open (design) | | `loop::test_dropping_an_unclosed_pool_keeps_it_running_until_its_loop_ends` |
| 6 | 10,000 concurrent tasks, 25% cancelled, a quarter failing | correct results, stats idle, no loop errors | no | | `pool::test_ten_thousand_tasks_with_cancellations_and_errors` |
| 6 | connections killed (`pg_terminate_backend`) at `max_size` under load | only statements on killed sessions fail, with connection-level errors; the pool recovers | no | | `pool::test_connections_dying_at_max_size_under_load` |
| 6 | shared statements vs callers that keep re-acquiring | shared statements starved forever | **yes** | `5789e07` | `pool::test_shared_statements_are_not_starved_by_repeated_acquires` |
| 6 | `acquire()` vs a flood of shared statements | served at once | no | | `pool::test_acquire_is_not_starved_by_a_flood_of_shared_statements` |
| 6 | `max_lifetime` / `max_idle` retirement under load | no failures; replaced; shrinks to `min_size` | no | | `pool::test_retirement_while_busy` |
| 6 | invalid configs | sizes rejected; negative `max_lifetime` / `max_idle` accepted (every connection expired at once), 0 silently meant never, NaN / inf leaked `min_size` connections, negative cache size an `OverflowError` | **yes** | `4d7d025` | `misuse::test_invalid_pool_configs_are_refused_before_connecting` |
| 7 | `into=int`, a Struct instance, a list | `TypeError` naming the accepted kinds | no | | `misuse::test_into_must_be_a_row_type` |
| 7 | non-str query on a connection | `TypeError: 'int' object is not an instance of 'str'` | no (message could name the argument) | | `misuse::test_query_must_be_a_str` |
| 7 | non-str query on a pool | connection lost from the idle stack, so later statements hung; when queued, the error hit whichever caller freed the slot | **yes** | `186e543` | `misuse::test_a_pool_query_that_is_not_a_str_*`, `misuse::test_a_waiting_pool_query_*` |
| 7 | `params=` with a t-string; `execute_script` with parameters | clear `TypeError`s (`$1` in a script: the server's error) | no | | `misuse::test_params_with_a_t_string_is_refused`, `misuse::test_execute_script_takes_no_parameters` |
| 7 | empty / whitespace / comment-only query | `select_one`: `NoRowsError`; `select_one_or_none`: `None`; `select_many`: `[]`; `execute`: 0; script: `None` | no | | `misuse::test_empty_and_comment_only_queries` |
| 7 | several statements in `select_one` / `execute` | `SQLSyntaxError` "cannot insert multiple commands into a prepared statement" | no | | `misuse::test_multiple_statements_in_a_prepared_query` |
| 7 | unknown `strategy=` | silently decoded as `"json"` | **yes** | `bd691f9` | `misuse::test_an_unknown_strategy_is_refused*` |
| 7 | wrong parameter count, NUL byte in the query | `ParameterError`, `InterfaceError` | no | | `misuse::test_wrong_parameter_counts_and_nul_bytes` |

`cancel::` is `tests/test_edge_lifecycle_cancel.py`, `close::` `..._close.py`, `tx::`
`..._tx.py`, `loop::` `..._loop.py`, `pool::` `..._pool.py`, `misuse::` `..._misuse.py`.

## The fixes

**A cancelled connect closes its socket** (`70c3849`). `close()` during the handshake
(which is what `connect()` does when cancelled) used to only mark the connection closing:
"closes itself once established". A server that never answered kept the handshake task,
the socket and its loop registration around for good, and there is no connect timeout but
`asyncio.timeout`. The handshake now races a `tokio::sync::Notify` that `close()` fires
(`notify_one` leaves a permit, so a close before the first poll counts too); the task then
fails `connected`, marks the connection dead and the socket is detached and closed within
the same `close()` call.

**`Transaction` query methods refuse to run outside the block** (`4903cdc`). The
transaction ran its queries on the connection's own native handle, so after the block
they ran in autocommit. The native `Connection` can now hand out a *transaction view*
(`transaction_view()`): the same `Shared`, plus an `AtomicBool` checked in
`require_client`, which every operation calls. The view starts inactive, is activated once
BEGIN succeeds and deactivated as the block exits (before COMMIT / ROLLBACK, which, like
SAVEPOINTs, go through the session handle). Cost: one atomic load per operation, on views
only.

**Pool options validated before connecting** (`4d7d025`): `max_lifetime` / `max_idle` must
be `None` or positive and finite, `statement_cache_size >= 0`. `0` used to mean "never";
it is now an error that points at `None`.

**Unknown `strategy=`** (`bd691f9`): `ValueError`, raised on the msgpack/json branch only.

**Loop moved to another thread** (`f139bf8`). The executor's loop thread was fixed at
creation. It's now an atomic thread key (a `thread_local` counter, since
`ThreadId::as_u64` is unstable), moved by the loop's own callbacks (`on_readable`,
`on_writable`, `flush`, `drain`), which only run on the thread running the loop. Off-thread
wake-ups still go through the mailbox.

**Loop closed with connections open** (`8ac641d`). The driver task holds the connection's
`Shared`, which holds the task; with no more readiness callbacks it never finished, so the
socket, the mailbox socketpair and the poker's clone leaked. `__del__` on a closed loop now
calls `abandon()`: close as before (the Terminate is still written if the socket takes it),
then drop every task unpolled (`Executor::clear`, which drops them outside the slots lock)
and detach the socket. `Executor::run` looks slots up with `get_mut`, as a waker can now
outlive its slot.

**`__del__` on another thread** (`d6f7037`). The close dropped the client there, which
woke the driver from that thread (a byte in the mailbox), and then removed the mailbox's
reader from that thread too, so the loop never looked. `__del__` now closes directly when
it runs inside its loop, and otherwise hands the close to the loop with
`call_soon_threadsafe` (or `abandon()` if the loop has closed).

**fork()** (`e6a2915`). An `os.register_at_fork(after_in_child=...)` hook bumps a fork
generation in Rust; a connection from an earlier generation is *inherited*: `pump()` never
polls it (so nothing is read or written on the shared socket), every operation raises
`InterfaceError` ("opened by another process ... open a new connection in this process"),
`closed` is true, `close()`, `abandon()` and `__del__` send nothing and leave the loop
alone (on Linux the child's epoll set is the parent's). Cost: one relaxed atomic load per
pump and per operation.

**Acquire starvation of shared statements** (`5789e07`). `_dispatch` now serves waiting
shared statements before `acquire()` waiters. A released connection takes the queued
shared statements, `_rebalance` then drains it for the next `acquire()` waiter, which gets
it once those finish: each side waits at most a round of the other. Nothing changes when
only one kind is waiting.

**`close()` cancelled** (`beec020`). `close()` awaits the shared `_closed` future through
`asyncio.shield`, and releases the mailbox registration in a `finally`.

**Pool query that isn't a str** (`186e543`). `Router.begin()` raises `TypeError` for it,
after the connection was taken off the idle stack. Both call sites now put the connection
back (`_place`) and fail the statement's own caller.

No benchmark was rerun: the hot-path additions are one atomic store per loop callback and
one or two atomic loads per operation.

## Open issues and proposed designs

### Uncollectable cycles through a native connection

Rust holds strong references to Python objects: each in-flight request's future (in its
executor task), and the loop hooks (`functools.partial`s holding bound methods of the
native object itself). The native type doesn't implement `__traverse__`, so the garbage
collector can't see through it. While the loop runs this is fine: requests finish and the
hooks are dropped on detach. But when the loop closes while a task still awaits a native
future, the cycle *task → coroutine → Connection → native → (Rust) request → future →
done-callback → task* is never collected: the connection, its 4 descriptors and its
session leak, without a `ResourceWarning`.

The pool hits it at `asyncio.run()` shutdown: cancelling a shared query spawns the pool's
ghost-flush task (`_flush_ghosts`, which waits on a no-op to learn when the server is past
the cancelled statement), created after `asyncio.run` took its snapshot of tasks to
cancel; it stays pending forever (xfail test above). A user task left pending by a bare
`loop.close()` does the same.

Proposed: make the native `Connection` a GC participant. `Shared` keeps a registry of the
futures it holds (a slab indexed by request id, entries removed when resolved) next to the
hooks; `__traverse__` visits both; `__clear__` does what `abandon()` does (drop the tasks
unpolled, drop the hooks). The collector can then find and break these cycles, and
`Connection.__del__` (via the cleared native) frees the socket. Narrower, pool-only option:
skip the ghost flush when the loop is shutting down; there's no public signal for that,
so the GC route is the real fix.

### An unclosed pool isn't reported

The upkeep task (`_maintain`) holds the pool, and the loop holds the task, so dropping the
last reference to an open pool closes nothing and warns nothing; its connections stay open
until the loop ends (then each warns). Proposed: give `Pool` a weakref slot
(`@dataclass(slots=True, weakref_slot=True)`), have `_maintain` hold only a `weakref` to it
(re-resolved each tick, exiting when it's gone), and add `Pool.__del__`: a
`ResourceWarning("unclosed fastpg pool")` plus closing its connections the way
`Connection.__del__` does.

### Use from another loop or thread is caught late

`conn.select_one()` from another loop raises asyncio's `RuntimeError` "attached to a
different loop", but only when awaiting: the statement was already submitted and runs. A
write through the wrong loop therefore happens while the caller sees an error. A pool used
from two threads isn't thread-safe at all (documented in `docs/limitations.md`). Proposed:
refuse at submission. The cheap half is in Rust: an operation submitted from a thread other
than the loop's raises `InterfaceError` (the thread key is already compared in `pump`).
Another loop on the same thread needs `asyncio.get_running_loop() is self._loop` per call
(~50 ns), which could sit behind a debug flag.

### A connection from `pool.acquire()` still works after its block

Documented ("don't keep using `conn` after the block"), but it's the same hazard the
transaction view fixed: statements land on a connection the pool has handed to someone
else. Proposed: yield a `Connection` built on a view of the native handle (the
`transaction_view` mechanism, generalised) and deactivate it on release.

### Minor

* A non-str query's `TypeError` ("'int' object is not an instance of 'str'") doesn't name
  the argument: the native methods could take `&PyAny` and cast with a message like "query
  must be a str or a t-string, got int" (error path only).
* A cancelled `SAVEPOINT` may still create the savepoint server-side; the next
  `savepoint()` reuses the name, and it goes away with the transaction. Harmless.
* Cancellation never reaches the server (documented): a cancelled COMMIT may or may not
  have committed, and a cancelled query keeps its connection busy until it ends.
