# Connection pool and transactions

Branch work for `fastpg.create_pool`, `Pool`, `Connection.transaction()` / `Transaction`,
plus one native fix the pool depends on. Code: `src/fastpg/_pool.py`,
`src/fastpg/_connection.py` (`Transaction`, `Connection.transaction`),
`native/src/connection.rs` (`dead` signal). Tests: `tests/test_pool.py`, plus one test in
`tests/test_connection.py`. Benchmark: `bench/mixed.py`.

## API

```python
pool = await fastpg.create_pool(
    dsn,
    min_size=1,  # kept open even when idle
    max_size=16,  # upper bound; grows on demand
    max_inflight_per_conn=8,  # shared statements pipelined per connection (1 = asyncpg-like)
    statement_cache_size=256,  # per connection, as in connect()
    max_lifetime=3600.0,  # replace a connection once idle after this many seconds (None: never)
    max_idle=300.0,  # close idle connections above min_size after this (None: never)
)

# Shared mode: no checkout, each statement autocommits, routed + pipelined.
await pool.select_one(query, *params, into=Person)  # also select_one_or_none, select_many
await pool.execute(query, *params)  # -> rows affected
await pool.execute_script("CREATE ...; INSERT ...")  # one implicit transaction

# Exclusive mode: the connection leaves shared rotation until the block exits.
async with pool.acquire() as conn:  # a plain fastpg.Connection
    await conn.execute(...)

# Transactions (exclusive by construction): COMMIT on success, ROLLBACK on exception.
async with pool.transaction(isolation="serializable", readonly=True, deferrable=True) as tx:
    await tx.select_many(...)  # the same query methods as a Connection
    async with tx.savepoint():  # nests; rolled back if the block raises
        ...

async with conn.transaction() as tx:
    ...  # same, on a Connection you hold

pool.stats()  # PoolStats(size, opening, idle, acquired, in_flight, waiting, waiting_acquire)
pool.closed
await pool.close()  # or: async with await create_pool(...) as pool
```

New exports: `Pool`, `PoolStats`, `create_pool`, `Transaction`, `IsolationLevel`
(`"read_uncommitted" | "read_committed" | "repeatable_read" | "serializable"`).

## Routing algorithm (shared statements)

On every `pool.select_*/execute/execute_script`:

1. **Idle first.** The shared connection that most recently ran out of work takes the
   statement. Idle connections are kept on a stack (LIFO), so finding one takes constant
   time however large the pool, the busy working set stays on the same connections, and
   surplus ones sit untouched at the bottom until `max_idle` closes them. (Until October
   2026 routing scanned the connection list in creation order instead, which cost about
   9 µs per statement with 100 mostly busy connections.)
2. **Grow without blocking.** If no connection is idle and `size + opening < max_size`,
   one more connection starts opening in the background. The statement doesn't wait for it.
3. **Pipeline onto the least stalled connection under the cap.** Among shared connections
   with fewer than `max_inflight_per_conn` statements in flight, pick the lowest
   `score = max(expected_ns, age)`:
   * `expected_ns` is the sum, over the connection's in-flight statements, of each SQL
     text's learned service time: an EWMA (α = 1/4) of `completion − max(sent, previous
     completion on that connection)`, i.e. its own execution time, excluding time spent
     queued behind others. Unknown SQL starts at 50 µs. The table is keyed by SQL text and
     cleared if it ever exceeds 10,000 entries (e.g. generated SQL).
   * `age` is how long the oldest in-flight statement has been running. It covers
     statements that overrun their estimate, or have none.
4. **Otherwise wait, FIFO.** When a slot frees, `_dispatch` hands it straight to the first
   waiter, so later arrivals can't barge in, and sends the waiter's statement right there
   (see "Queued statements" below). New arrivals queue behind existing waiters even if a
   slot is momentarily free.

`acquire()` / `transaction()` need a fully idle connection. They take an idle one if any;
otherwise they wait FIFO in a separate queue, growing the pool if allowed. If the pool
can't grow, as many busy connections as there are acquire waiters are marked *draining*:
they get no new shared statements, so they go idle soon and are handed to the waiter.
Without draining, an acquire could starve under constant shared load.

## Design decisions and assumptions (each with why)

1. **Pure Python, single event loop, no locks.** Every state change happens on the loop
   thread between awaits. The pool adds no measurable cost: `fastpg-pool x1 cap 64` vs a
   bare pipelined connection measured 28.2k vs 28.3k ops/s at 16 tasks, 42.6k vs 42.8k at
   64, and 43.7k vs 44.1k at 256. Nothing on the hot path needed Rust. *Superseded
   (October 2026, `notes/perf-pool.md`):* measured as CPU per statement, the Python
   bookkeeping cost ~1.1 µs per statement on the idle path and ~3.4 µs when scoring 16
   busy connections, so the statements in flight, the learned costs and the scoring moved
   to `fastpg._native.Router`; the rest of routing is still Python on the one loop.
2. **Growth doesn't block a statement (rule 2).** Connect takes a median **3.5 ms** (max
   16 ms) on this host, against 0.34 ms for a point lookup. Pipelining onto an existing
   connection costs at most the queue ahead of it, and the stall score avoids long queues.
   When every connection is at the cap, a waiter is served by whichever comes first: a
   freed slot or the new connection.
3. **Default `max_inflight_per_conn = 8`, from the measurements below.**
   * Cap 1–2 kills the pipelining win: 15k / 25k ops/s against 51k at cap 8, with 64
     tasks on 8 connections. Under the mixed load they collapse at 20k/s (11k / 16k
     achieved, 0.5–2 s p50).
   * Cap 4 holds 20k/s but costs throughput (38k ops/s at 64 tasks) and p50 (1.3 ms at
     20k/s).
   * Cap 8 keeps nearly all of the pipelining throughput: 51k vs 51–57k uncapped.
   * Cap 8 has the best p99.9 among caps at 10k/s on 8 connections (8.9 ms, vs 18.6 ms at
     cap 4).
   * Cap 8 bounds how many statements can queue behind any one slow statement.
   * Cap 16 has similar tails but lets one connection absorb a bigger burst.
4. **Learned-cost stall score instead of "oldest age + per-query cost".** The Linux
   result was p99.9 of 17–19 ms with stall-aware routing, because a fresh slow statement
   looks exactly like a fresh fast one. Interleaved A/B on this host, 10k/s, 1% `pg_sleep(0.02)`,
   x8, three rounds each:
   * age + n·50 µs (lead's scorer): fast p99.9 **20.7–21.7 ms** at every cap; p99 1.0–3.1 ms.
   * `abs(expected − age)` (tried first): p99.9 12–18 ms, but p99 rose to 1.5–4.8 ms. A
     connection whose 20 ms statement is "almost done" scores as free, and estimates
     overrun.
   * **`max(expected, age)` (shipped):** p99.9 **9.1–11.0 ms at cap 8, 5.4–17.5 ms at
     cap 16**, p99 back to ~1.0 ms, slow-query p99 also lower. It doesn't cost throughput
     (50.8k ops/s at 64 tasks, same as before).
   * At 20k/s on 8 connections both scorers are similar (p99 ≈ 20 ms). That load needs
     more connections, and with **16 connections at cap 8 fast p99 is 1.4 ms and p99.9
     2.3 ms at 20k/s**, an asyncpg-like tail at a rate asyncpg x32 can't sustain.
   * `tests/test_pool.py::test_avoids_connection_behind_known_slow_statement` pins the
     behaviour; it fails with the age-only scorer.
5. **`acquire()` yields the plain `fastpg.Connection`**, not a proxy. That means zero
   overhead and code typed for `Connection` works unchanged. The caveat (documented): don't
   use it after the block. A connection the user closed is detected on release and dropped.
6. **Session reset on release: one round trip, no `DISCARD ALL`.** One simple-protocol
   script:
   * a transaction guard (point 7);
   * `SELECT pg_advisory_unlock_all(); CLOSE ALL; UNLISTEN *; RESET ALL; RESET SESSION
     AUTHORIZATION; RESET ROLE; DISCARD TEMP`.

   This is everything `DISCARD ALL` does except `DEALLOCATE ALL` and `DISCARD PLANS`, so
   fastpg's prepared statements and plans survive, which is the point of the statement
   cache. Measured on this host: the script takes 343 µs, the same as `SELECT 1` (338 µs),
   i.e. one RTT. `RESET ALL` alone was measured **not** to reset `SET ROLE` (hence the
   explicit `RESET ROLE` / `RESET SESSION AUTHORIZATION`). `DISCARD TEMP` added ~10 µs,
   within noise. asyncpg's default reset is the same idea minus role/temp.
7. **Detecting a transaction left open, without WARNING spam.** fastpg can't see the
   ReadyForQuery transaction status (xitca 0.4 discards it), and a blind `ROLLBACK` logs
   "there is no transaction in progress" on every release. The guard is
   `SELECT (CASE WHEN now() = statement_timestamp() THEN '0' ELSE 'fastpg: connection
   released inside a transaction' END)::int`.
   * In the simple protocol PostgreSQL sets `xactStartTimestamp = stmtStartTimestamp` for
     an implicit transaction, so the two are exactly equal unless a transaction was already
     open. Verified: 2,000/2,000 autocommit passes, and detection in both open and failed
     (25P02) transactions. It does **not** work over the extended protocol, where it is
     always false.
   * If the guard trips, the script aborts before resetting, and the pool runs
     `ROLLBACK; <reset>`. If that fails too, the connection is closed, not returned.
8. **`pool.transaction()` folds the reset into COMMIT's round trip**, *before* `COMMIT`, so
   a failure means nothing was committed. On the exception path it's ROLLBACK plus the
   guarded reset (2 RTT, error path only).
9. **`Transaction` shares `Connection`'s query methods.** Both subclass the private
   `_Queries`, so `tx.select_one(...)` etc. need no wrappers, and both inherit whatever is
   added there. `Transaction` is a plain `__slots__` class like its base, not a dataclass
   (style guide §1 deviation: it has a custom `__init__` and slots).
10. **COMMIT of an aborted transaction raises instead of silently rolling back.** The
    COMMIT script is `SELECT 1; COMMIT` (or `<reset>; COMMIT` in the pool). If the block
    swallowed a server error, the first statement fails with 25P02, the transaction is
    rolled back, and the block raises that `DatabaseError` with a note. Plain PostgreSQL
    would answer `COMMIT` with `ROLLBACK` and the caller would believe it committed.
11. **Concurrent use of a `Connection` during `conn.transaction()` is documented, not
    guarded.** Guarding would put a check on every query call for everyone. Only the
    cheap and common mistake is guarded: a second `conn.transaction()` while one is active
    raises `InterfaceError` ("use tx.savepoint()").
12. **Cancelled shared statements keep their slot until the server is past them.** A
    cancelled statement still runs server-side. Freeing its slot immediately would make
    the connection look idle and route new work behind, say, a 30 s query. Instead it stays
    in flight as a *ghost* until a `SELECT 1` sent after it completes; ghosts are batched,
    one sentinel per connection at a time. Tested.
13. **Dead connections are replaced lazily, with no retries.**
    * A connection found `closed` when picked is dropped.
    * A statement failing with a session-fatal error (`ConnectionClosedError`, SQLSTATE
      class 08, 57P01/57P02/57P03) drops its connection immediately, before EOF is even
      seen.
    * The pool then grows back on demand, and to `min_size`.
    * Statements in flight fail with the existing error types. Nothing is retried: a
      statement may have executed before the connection died.
14. **Native fix: operations on a dead connection fail instead of hanging forever.**
    * Found while testing replacement, 3/3 reproducible: a query sent right after another
      one received the server's FATAL 57P01, but before EOF was processed, **hung forever**.
      So did queries pipelined behind the one that received the FATAL. Only the head
      request gets the FATAL error.
    * Cause, in xitca 0.4 `driver/generic.rs`: dropping the driver only sets `closed = true`.
      Pending response senders stay in state shared with the `Client`, and every waiting
      operation holds an `Arc<Client>`. That is a circular wait.
    * Fix: a `tokio::sync::watch` "dead" flag per connection, set when the connection task
      ends. Every operation (`submit`, `execute_script`) races against it, so it fails with
      `ConnectionClosedError` and drops its client clone.
    * Cost: one `watch::subscribe` plus one extra select arm per operation. No measurable
      throughput change.
    * Covered by `test_pipelined_and_racing_queries_fail_when_server_closes`. This affected
      bare connections too, e.g. any pipelining app during a server restart.
15. **`max_lifetime` / `max_idle` upkeep** run in one maintenance task, every
    `min(10 s, max_lifetime/4, max_idle/4)`.
    * Expired connections are closed when idle. Busy ones are marked *retiring*: no new
      shared work, closed when idle.
    * Idle time counts from when the connection last became idle.
    * The task also refills to `min_size`.
16. **`close()` is graceful.**
    * New calls and all waiters fail with `ConnectionClosedError("pool is closed")`.
    * Shared statements in flight complete; idle connections close.
    * Checked-out connections close when released, and `close()` waits for them.
    * It's idempotent and shielded: cancelling one `close()` caller doesn't abort the close.
17. **A failed background connect fails the waiters only if nothing else can serve them**
    (no live and no opening connections). Otherwise they keep waiting for slots.
    `create_pool` raises if any of the `min_size` initial connects fails, and closes the
    others.
18. **`Pool` is a `@dataclass(slots=True, eq=False)`** (style guide §1). Its config fields
    are public and read-only by convention. Module-level constants were moved to
    `ClassVar`s (§9). The single `# noqa: ANN401` on `Pool._query -> Any` is a ruff
    annotation rule, not a type-checker suppression; the public overloads carry the types.

## Benchmark numbers

Host: Apple-silicon Mac, Docker Desktop port-forwarding (~0.33 ms RTT), PostgreSQL 18.6,
uvloop, DB `fastpg_pool`. The machine was shared with other workloads (load average ~11). The
first throughput run showed transient dips; every number below was reproduced at least
twice. **These should be rerun on Linux.**

### Pure point lookups, closed loop (`bench/results/mixed-throughput-20261010T092630Z.json`)

```sh
FASTPG_DSN=postgresql://fastpg:fastpg@127.0.0.1:55432/fastpg_pool uv run yeet ./bench/mixed.py \
  --throughput-only -c 1 -p 8 -k 1 -k 2 -k 4 -k 8 -k 16 -k 64 -a 16 \
  -o 'fastpg x1' -o 'fastpg-pool*' -o 'asyncpg x16' -n 16 -n 64 -n 256 -t 3
```

| config | 16 tasks | 64 tasks | 256 tasks |
|---|---:|---:|---:|
| fastpg x1 (one pipelined connection) | 28,136 | 42,651 | 44,107 |
| fastpg-pool x8 cap 1 | 14,815 | 14,756 | 14,824 |
| fastpg-pool x8 cap 2 | 25,096 | 24,670 | 24,374 |
| fastpg-pool x8 cap 4 | 24,301 | 38,102 | 38,143 |
| **fastpg-pool x8 cap 8** | 24,359 | **50,803** | **50,228** |
| fastpg-pool x8 cap 16 | 24,178 | 51,299 | 56,650 |
| fastpg-pool x8 cap 64 | 24,220 | 51,174 | 57,090 |
| asyncpg x16 | 10,126 | 10,256 | 10,287 |

At 16 tasks, one connection beats eight (28k vs 24k): with only two statements per
connection there's less batching per wake-up. At 64+ tasks, eight connections win.

### Mixed: 1% `pg_sleep(0.02)`, rest point lookups (`bench/results/mixed-20261010T092813Z.json`)

```sh
FASTPG_DSN=postgresql://fastpg:fastpg@127.0.0.1:55432/fastpg_pool uv run yeet ./bench/mixed.py \
  -c 8 -p 8 -p 16 -k 1 -k 2 -k 4 -k 8 -k 16 -a 16 -a 32 \
  -o 'fastpg-aware x8' -o 'fastpg-pool x8 *' -o 'fastpg-pool x16 cap 8' -o 'asyncpg*' \
  -r 5000 -r 10000 -r 20000 -t 5
```

Fast-query latency, p50 / p99 / p99.9:

| config | 5k/s | 10k/s | 20k/s |
|---|---|---|---|
| fastpg-aware x8 (lead's stall-aware) | 0.45 / 0.93 / 1.3 ms | 0.46 / 1.2 / 20.8 ms | 0.48 / 37.8 / 59.3 ms |
| pool x8 cap 1 | 0.46 / 1.2 / 1.8 ms | 0.73 / 9.4 / 10.4 ms | overloaded (11k achieved, 2 s p50) |
| pool x8 cap 2 | 0.47 / 1.1 / 14.4 ms | 0.47 / 8.0 / 20.6 ms | overloaded (16k achieved, 0.5 s p50) |
| pool x8 cap 4 | 0.46 / 0.97 / 1.2 ms | 0.46 / 1.1 / 18.6 ms | 1.3 / 23.5 / 35.1 ms |
| **pool x8 cap 8** | 0.46 / 0.93 / 1.2 ms | 0.46 / 0.96 / **8.9 ms** | 0.58 / 20.7 / 31.7 ms |
| pool x8 cap 16 | 0.46 / 0.94 / 1.3 ms | 0.46 / 0.97 / 5.4 ms | 0.56 / 20.4 / 39.0 ms |
| **pool x16 cap 8** | 0.46 / 0.95 / 1.5 ms | 0.47 / 0.99 / **1.6 ms** | 0.66 / **1.4 / 2.3 ms** |
| asyncpg x16 | 1.0 / 2.3 / 5.2 ms | overloaded (77 ms p50) | overloaded |
| asyncpg x32 | 1.0 / 2.2 / 4.9 ms | 1.6 / 3.6 / 6.0 ms | overloaded (12k achieved) |

Earlier interleaved A/B of the scorers (same command shape, `-p 8 -k 4 -k 8 -k 16 -r 10000
-t 5`, run 3× per variant by swapping `_pool.py`) is summarised in decision 4. Other
measurements:
* Connect time: `fastpg.connect` median 3.5 ms, max 16 ms (30 samples).
* Release reset script: 343 µs vs `SELECT 1` 338 µs (500 samples).

## Queued statements: sent by the code that frees the slot (October 2026)

**Symptom.** Point lookups by uuid primary key into an 11-field Struct, `N` tasks looping on
`pool.select_one`, pool of `C` (`min_size = max_size`, cap 8), uvloop, PostgreSQL 18 on the
host (not Docker). At exactly 8 tasks per connection: ~71k ops/s. With more, so that
statements wait in `_waiting` (rule 4): ~41k, flat, whatever `C`.

**Cause.** `_dispatch` reserved a freed slot for the first waiter and resolved its future;
the waiter's task sent its statement when the loop next ran it. By then the write coalescing
had been missed: a drain that resolves several results schedules a `flush()` behind the woken
tasks, and the queries they submit before it runs are written together. The waiters ran an
iteration later, after that flush, so each waiting statement was written with a `sendto` of
its own (and the connection ran below its cap meanwhile). Evidence, 16 connections, base
`72052f6`, 256 tasks (waiting) vs 128 (at the cap):

* The loop thread is saturated in both (99.4% busy): 24.4 vs 14.2 µs of CPU per op.
* cProfile: `_native.Connection.fetch` costs **19.1 µs per call** waiting vs 0.47 µs at the
  cap (it writes and polls the socket itself instead of queueing for the flush).
* macOS `sample` of the loop thread: `__sendto` is 68% of samples, **17.2 µs per op**, all
  of it under `Connection::submit`; at the cap 5.1 µs per op, mostly from the flush.
* The Python bookkeeping (`_rebalance`'s list comprehensions on every dispatch, the extra
  future, deque operations, rule 4's check) is ~2 µs per op of the difference, not 10.

**Fix.** `_dispatch` sends the waiting statement itself (`Pool._hand`), from the task whose
completion freed the slot: before that connection's scheduled flush, so the refills are
written together, as a task's own next statement is at the cap. The waiter's future is the
statement's native result future (`Fetcher.fetch` / `send_execute` / `send_script` take it),
so the caller wakes once, when the result is in. A statement that fails before it's sent
(e.g. an unavailable `strategy`) releases the slot in `_hand` and fails its own caller.
Cancelled while queued: removed; once sent: a ghost (decision 12); result already in: slot
released. Routing is unchanged: FIFO, no overtaking, the same slot choice.

**Result** (a pool load test, 250k requests, 5 interleaved rounds, median [min..max]):

| tasks per connection | base ops/s | p50 / p99 | fix ops/s | p50 / p99 |
|---|---:|---|---:|---|
| 16 × 128 (at the cap) | 69,955 [69,142..71,686] | 1.84 / 2.40 ms | 72,334 [69,644..73,616] | 1.76 / 2.37 ms |
| 16 × 256 | 41,023 [40,707..42,550] | 6.09 / 7.35 ms | **100,507** [99,640..102,245] | 2.48 / 3.30 ms |
| 8 × 128 | 40,314 [39,876..41,263] | 3.12 / 3.80 ms | **88,678** [86,951..90,237] | 1.43 / 1.94 ms |

At 16 × 256, `__sendto` is now 2.2 µs per op, none of it from a submit. `bench/mixed.py`
(compose PostgreSQL, the mixed command above restricted to `-o 'fastpg-pool x8 *' -o
'fastpg-pool x16 cap 8'`, two runs each, interleaved): wherever the load is sustained, fast
p50/p99 and slow p99 are within run-to-run noise (x8 cap 8 at 10k/s: 0.42/0.85 and 0.42/0.83
ms before, 0.43/0.86 twice after; x16 cap 8 at 20k/s: p99 1.45/1.28 ms before, 1.37/1.55
after; the noisiest, x8 cap 2 at 10k/s: p99 4.5/5.9 ms before, 6.8/5.9 after). The overloaded
configs at 20k/s improve (cap 2: 17.8k achieved instead of 16.5k, p50 253 ms instead of 473;
cap 1: 11.8k instead of 11.4k). Its closed loop (`--throughput-only -c 1 -p 8 -k ... -n 16
-n 64 -n 256 -t 3`, one run each), `fastpg-pool x8 cap 8` at 256 tasks: 38.2k → 76.0k ops/s
(64 tasks, at the cap: 80.2k → 79.0k).

**Throughput follows batch size, not Python CPU.** Once the queue works, the loop thread
spends its time on per-connection `sendto`/`recvfrom` pairs, so what counts is how many
results each loop iteration handles, and doing *less* per statement can measure *slower*
(16 × 256): skipping `_rebalance` when no `acquire()` waits (~0.25 µs saved per dispatch)
ran 87k ops/s at 11 ops per loop iteration, against 99k at 26 with it, and a busy-wait of
250 ns per dispatch in its place gives the same 100k (600 ns: 108k, at 100 ops per
iteration). Likewise, with that gate, waking the waiter twice (its own future, then the
statement's) ran 98k. So the gate isn't in; the single wake is kept for its lower CPU per
statement and simpler cancellation. On this host a deliberate coalescing delay in the
native read/flush path may be worth more than any per-statement saving (open issue below).

Tests: `test_waiting_statements_run_in_arrival_order` (FIFO with three pipelined, by
`nextval`), `test_waiting_statement_is_sent_by_the_statement_that_frees_its_slot` (fails on
the old code), `test_waiting_statements_errors_reach_their_callers` (unsent, server and
decode errors, `execute`, `execute_script`), `test_cancelled_waiter_keeps_its_slot_once_sent`
(fails on the old code, which never sent it),
`test_cancelled_acquire_waiter_undrains_the_connection`.

## Open issues / things I'm unsure about

* **All numbers are macOS-through-Docker**, where RTT (~0.33 ms) dominates. On Linux, with
  ~50 µs RTT, the fast statements' learned cost will be smaller and the cap may matter at
  higher rates. Please rerun `bench/mixed.py` with the commands above in the Linux
  container.
* **`max_size` defaults to 16.** It was 10 (asyncpg's) when this was written, but the data
  says tails at ≥10k/s with 1% slow queries want ~16 connections, and connections only open
  when every existing one is busy, so the default was raised later.
* **A coalescing delay in the native read/flush path?** Under a saturated closed loop on this
  host, throughput tracks results per loop iteration (see "Queued statements"): an idle
  600 ns per dispatch raised 100k to 108k ops/s. Waiting a little before reading or
  flushing would trade latency at low load for fewer syscalls at high load; not measured
  on Linux, where syscalls are cheaper.
* **Statements pipelined concurrently on one connection can execute out of submission
  order.** This is pre-existing fastpg behaviour: on a statement-cache miss the Parse round
  trip happens first, so a later cached statement can overtake it (seen in a FIFO test
  with cap > 1). It doesn't affect pool correctness. It does make the cancelled-statement
  sentinel (decision 12) a heuristic, which could free a ghost slot early. It's also worth
  knowing for anyone pipelining dependent statements without awaiting.
* **No server-side cancel.** A cancelled shared statement keeps running, and its
  connection stays busy until it finishes; that's deliberate, see decision 12. Cancelling
  inside `pool.transaction()` rolls back only after the running statement ends. Adding
  PostgreSQL CancelRequest support (xitca has `cancel.rs`) would fix both.
* **No connect timeout and no backoff.** With the server down, each statement that finds
  the pool below `max_size` starts a connect attempt (bounded by `max_size`). Use
  `asyncio.timeout` around calls for now.
* **Shared statements that change session state** (`SET`, `BEGIN`, temp tables) aren't
  detected. That's documented on `Pool`; use `acquire()`.
* **`BEGIN` costs a round trip.** It could be pipelined with the first statement, since
  BEGIN can only fail if the connection is dead. Not done: subtle, and the win is 1 RTT per
  transaction.
* **Merge notes.**
  * `Pool` mirrors `Cursor`'s shared-mode methods by hand (`select_one`,
    `select_one_or_none`, `select_many`, `execute`, `execute_script`). When the t-string
    and bulk-write branches land, `Pool` needs matching wrappers if they should be usable
    in shared mode: a new kind constant in `Pool._call`, plus an overload. `Transaction`
    inherits them automatically.
  * `execute_many` / `copy_in` probably belong in exclusive mode only (`acquire()` /
    `transaction()`), since a long COPY would block a shared connection's pipeline.
  * `native/src/connection.rs` changed in `Shared` (new `dead` field and `unless_dead`),
    the connect task (one line), `execute_script` and `submit`. Any new native operation
    from the bulk-writes branch should also be wrapped in `shared.unless_dead(...)`, or it
    can hang the same way.
* `inspect.signature(fastpg.Pool)` under the default VALUE format raises `NameError`
  because of the `TYPE_CHECKING`-only `Sequence` import. That's the same as every other
  function in the package with `TYPE_CHECKING` annotations, e.g.
  `Connection.__aexit__`; the STRING format works.
