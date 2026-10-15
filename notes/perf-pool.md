# Pool: per-statement overhead in shared mode

Branch work on what `pool.select_one(...)` costs beyond the same query on a connection the
task owns. Starting point (Linux, 16 tasks doing point lookups): `create_pool(16)` in shared
mode 64.4k ops/s against 72.3k for 16 pinned connections, 55.6k against 61.5k at 100 tasks,
i.e. ~1.5–2 µs of pool bookkeeping per statement on the event-loop thread.

Code: `src/fastpg/_pool.py`, `src/fastpg/_connection.py` (`Fetcher`),
`native/src/router.rs` (`Router`). Tests: `tests/test_pool.py`, Rust unit tests in
`router.rs`. Scripts and logs: `notes/eval/2026-10-pool-overhead/`.

## Result

| commit | change | idle path, ns/statement saved | 16 busy connections (scoring), ns saved |
|---|---|---:|---:|
| `8517665` | call the connection's fetch path directly (`Fetcher`) | **~210** | (same) |
| `0f6d5d2` | idle pick with one `popitem`, inline | ~95 | – |
| `c32a75b` | `_finish` fast path for a success on a shared connection | ~90 | – |
| `023428a` | scoring without an iterator per connection (`oldest_ns`) | 0 | **~1,100** |
| `b18f3a4` | per-statement routing state in Rust (`Router`) | **~245** | **~1,270** |

Ranking, by gain for what it costs:

1. **Router** (`b18f3a4`): the largest gain on both paths, but a native class and the
   routing state split across the boundary (the one subtlety: slot lifetime, see below).
2. **`Fetcher`** (`8517665`): ~210 ns on every row-returning statement for a 25-line class.
3. **Scoring without an iterator** (`023428a`): ~1.1 µs per statement whenever no
   connection is idle; superseded by the Router if that is kept, worth keeping otherwise.
4. **Inline idle pick** (`0f6d5d2`) and **`_finish` fast path** (`c32a75b`): ~90 ns each.

Commits 1–4 stand on their own if the Router isn't wanted (1,170 → 774 ns idle, 3.4 → 2.0 µs
scoring). Rejected: skipping the cost model's timestamps on the idle path, public methods
returning coroutines, a native pool, and pipelining earlier with a threshold relative to the
statement's own cost (see the sections below).

Pool overhead per statement, pure bookkeeping (hand-driven microbenchmark, see Method):
**1,170 → 774 ns** with the four Python commits, **→ 529 ns** with the Router (a later
rerun: 1,184 → 768 → 524). With 64 tasks on 16 connections (every statement scored):
**3,443 → 2,014 → 743 ns** (rerun: 3,731 → 2,177 → 822).

On the real database (this host, loop-thread CPU per lookup, pool minus pinned, median of
interleaved rounds):

| setup | before (`1e864ae`) | Python commits (`023428a`) | Router (`b18f3a4`) |
|---|---:|---:|---:|
| 16 tasks, pool of 16 | +2.40 µs [1.96..6.33] | +1.51 [0.51..4.37] | +1.08 [−0.44..5.68]; rerun +1.00 [0.90..1.22] |
| 100 tasks, pool of 100 | +2.41 µs [2.15..3.29] | +1.61 [1.17..2.01] | +1.31 [0.67..1.74] |
| 100 tasks, pool of 16 (pipelined) | 25.59 µs/op [24.53..26.31], 38.6k ops/s | 24.20 [23.05..24.36], 40.8k | **21.18 [20.97..21.92], 46.0k** |

Routing is unchanged: idle first (LIFO), grow below `max_size`, lowest
`max(expected, age)` under `max_inflight_per_conn`, FIFO waiters, learned per-SQL costs,
ghosts of cancelled statements, drain for `acquire()`. `tests/test_pool.py` has 58 tests,
6 of them new (below), all passing; the full suite: 420 passed, 17 skipped; `cargo test`
21 passed (5 new).

`bench/run.py` (`-s concurrent -s concurrent-pinned -d fastpg-struct -t 5`, 3 interleaved
runs each, median [min..max] ops/s; `run_ab.py`):

| tasks | pinned (base run) | pool, before | pinned (Router run) | pool, Router |
|---|---:|---:|---:|---:|
| 16, pool of 16 | 21,239 [20,340..21,453] | 21,199 [20,994..21,650] | 21,996 [21,180..22,245] | 21,518 [20,924..21,705] |
| 100, pool of 100 | 34,918 [34,361..35,094] | 32,398 [31,789..32,510] (−7.2%) | 35,179 [34,818..35,610] | 33,657 [33,576..34,274] (−4.3%) |

At 16 tasks both are RTT-bound here (no signal); at 100 the loop thread is closer to
saturation and the Router closes about 40% of the gap to pinned.

## Method

**Host-only measurements.** Apple-silicon Mac (14 cores), the compose PostgreSQL 18 at
`localhost:55432` through Docker Desktop's port forward (RTT 0.3–0.8 ms), Python 3.14.7,
uvloop. The machine was shared with other workloads (load average 9–20 during the runs), so
every comparison is **interleaved** (each round runs every variant once, in turn) and
reported as median [min..max] over the rounds.

On this host the closed loop is latency-bound: 16 tasks reach ~20k ops/s with the loop
thread 60–75% busy, so ops/s barely moves with CPU cost (see the `bench/run.py` table
above). The pool's overhead is CPU on the loop thread, so that is what is measured; on
Linux, where the loop thread is the bottleneck, CPU per statement is what converts into
ops/s.

Three instruments (scripts in `notes/eval/2026-10-pool-overhead/`):

1. **Hand-driven microbenchmark** (`micro2.py`): real `Connection` / `_Queries` code over
   a fake native object whose `fetch` does nothing; each statement's coroutine is driven by
   hand (send, resolve the future it yields, send), no event loop. Pool overhead = time per
   `pool.select_one_or_none(query, i, into=P, strategy="direct")` minus the same call on a
   bare `Connection`, 15–21 interleaved rounds of 200k statements. Spread ±5%. This is the
   idle path (one statement at a time), i.e. the `-c 16` / `-c 100` benchmark shape.
2. **Loop-driven microbenchmark** (`micro3.py`): the same fake native answering on the
   next uvloop iteration; TASKS tasks back to back through a pool of SIZE, minus TASKS tasks
   on SIZE connections assigned statically. With TASKS > SIZE nearly every statement is
   scored (rule 3): 95% at 64 tasks on 16.
3. **Real database** (`cpu_pool2.py`): `time.thread_time()` per lookup for pinned
   connections and each pool variant, interleaved, the `bench/` query and `Person` Struct.

Variants were whole `_pool.py` files loaded under another module name, so one process
compares them (`git show <commit>:src/fastpg/_pool.py > v/<name>.py`; the `mk_*.py` scripts
derive the ablations). Commands, from the repo root, `S=notes/eval/2026-10-pool-overhead`
(adjust the `<repo>` / `<this directory>` placeholders first):

```sh
uv run yeet $S/micro2.py -v base -v head -v installed --kw --rounds 21
uv run yeet $S/micro3.py -v base -v head -v installed --tasks 64 --size 16
uv run yeet $S/cpu_pool2.py -v base -v head -v router --tasks 16 --rounds 9
uv run yeet $S/cpu_pool2.py -v base -v head -v router --tasks 100 -n 50000 --rounds 7
uv run yeet $S/cpu_pool2.py -v base -v head -v router --tasks 100 --size 16 -n 50000 --rounds 7
```

## Where the time went (before, `1e864ae`)

Idle path, hand-driven, no `into=` (1,100 ns of pool overhead per statement), by ablation
(removing one piece at a time) and by building the path back up from a skeleton:

| piece | ns/statement |
|---|---:|
| two extra coroutine frames (`select_*` → `_query`) + idle stack pop/push | ~290 |
| routing checks, `isinstance(Template)`, `conn.closed`, waiter check | ~50 |
| begin/finish bookkeeping: token, 2× `perf_counter_ns` (~32 ns each), `started` dict + tuple, `expected_ns` | ~275 |
| learning the SQL's service time (`_learn`: `max`, `dict.get`, EWMA, store) | ~125–160 |
| `_route` → `_best_slot` → `_begin` calls, `next(reversed(idle))` iterator, second `idle.pop` | ~95 |
| `_place` on completion (class-constant lookups, `_drained.pop`) | ~80 |
| `_call`'s kind dispatch and `conn.select_*(query, *params, into=..., strategy=...)` | ~210 with `into=` (the `*params`/keywords call is 112 ns against 34 ns positional) |

Plus, when no connection is idle, **scoring: ~190 ns per shared connection per statement**
(`next(iter(started.values()))[0]` allocates an iterator, `max()` is a builtin call): 3.4 µs
per statement with 16 busy connections, more than the whole idle path.

Reference costs on this host (`crossing.py`, `callcost.py`, timeit best of 5): Python call
10 ns, slotted attribute 5 ns, `dict.get` 12 ns, `time.perf_counter_ns()` 32 ns,
`conn.closed` (property over a native getter) 29 ns, a PyO3 method call 18–20 ns.

## Candidates

### 1. `Fetcher`: no argument repacking on the way to the connection (`8517665`) — kept

`Pool._call` re-dispatched on the statement kind and called
`conn.select_one_or_none(query, *params, into=into, strategy=strategy)`: a
`CALL_FUNCTION_EX` that rebuilds the arguments tuple and a keywords dict, then binds them
again, plus one more coroutine layer, only to reach `_Queries._fetch(query, params, shape,
into, strategy)`. The pool can't call `_fetch` (pyright `reportPrivateUsage`), so each entry
holds a `Fetcher`, a `_Queries` subclass on the same native connection (in
`_connection.py`, not exported) whose `fetch()` is one plain call to `_fetch`.
**1,041 → 830 ns** (hand-driven, `into=` and `strategy=` passed). `execute` and
`execute_script` still go through the connection's methods (not on the measured path).
New test: `into=` / `strategy=` and their errors reach the connection for every
row-returning method.

### 2. Idle pick with one `popitem`, inline (`0f6d5d2`) — kept

Rule 1 went `_route` → `_best_slot` (peek with `next(reversed(idle))`) → `_begin` (pop the
same entry). `dict.popitem()` pops the top of the stack (LIFO) in one step; the idle case
is inlined in `_query`, and `_best_slot` pops too, so `_begin` no longer touches the stack.
**847 → 752 ns.** Dead idle connections are still dropped when picked (covered by
`test_dead_idle_connection_is_skipped_by_shared_statements`).

### 3. `_finish` fast path (`c32a75b`) — kept

A success on a SHARED connection goes straight back on top of the idle stack when nothing
is left in flight, skipping the error / RETIRING checks and `_place` (a SHARED entry is
never in `_drained`). **804 → 715 ns.** Inlining `_learn` too was 5 ns more: not worth the
method.

### 4. Scoring without an iterator per connection (`023428a`) — kept

Each entry keeps `oldest_ns`, the send time of its oldest statement in flight (set when a
statement starts on an empty connection, moved to the next one when the oldest completes);
the clock is read once per scoring pass. 64 tasks on 16 connections: **3,096 → 1,987 ns**;
idle path unchanged. New test: `test_age_counts_from_the_oldest_statement_still_in_flight`
(fails if the age keeps counting from a completed statement).

### 5. Routing state in Rust (`b18f3a4`) — kept: the crossing costs far less than it saves

The question was whether two or three PyO3 calls per statement cost more than the Python
they replace. Measured: a PyO3 method call is 18–20 ns here (30–40 ns with arguments),
against ~400 ns of Python bookkeeping per statement and ~120 ns per connection when
scoring. `fastpg._native.Router` (called from the loop thread only) keeps, per connection
slot, the statements in flight (token, send time, expected cost), the expected work, the
last completion time, and whether it is in shared rotation; plus the per-SQL cost table
keyed by the str's cached hash (identity first, then equality). The pool calls `begin` and
`finish` per statement and `best(cap)` when nothing is idle; idle stack, states, waiters,
drain, ghosts and growth stay in Python. Same score, same EWMA (with Python's floor
division), same default and table bound; `max_idle` uses the slot's last completion time,
and a release `touch`es it.

* Hand-driven idle path: **774 → 529 ns** (with `into=`), 762 → 522 ns without.
* On the loop: 16 on 16 **1,015 → 709 ns**, 100 on 100 **929 → 669 ns**, 64 on 16
  **2,014 → 743 ns**, 128 on 16 (waiters) **1,415 → 683 ns**.
* Real DB, 100 tasks on a pool of 16: **24.20 → 21.18 µs** CPU per lookup, 40.8k → 46.0k
  ops/s.

The one subtlety: a dropped connection's slot is freed only once nothing is in flight on
it (late completions of pipelined statements, ghosts), so they can never touch a reused
slot. New test: `test_pipelined_queries_fail_cleanly_when_their_connection_dies` (fails if
the slot is freed eagerly: the late completions raise `KeyError` instead of the connection
error). Rust unit tests cover service time after a previous completion, scoring under the
cap, the age moving on, and slot reuse.

Tie-break difference: Python scanned `_entries` in creation order, the Router scans slot
ids, which are reused after a connection leaves. Exact score ties (nanosecond scores)
aren't a documented rule.

### Rejected

* **Skip the cost model's timestamps when the picked connection was idle.** Upper bound
  ~285 ns in Python (ablation `no_time`), but learning needs both timestamps, and at low
  load *every* statement takes the idle path: the pool would never learn a cost, so a
  known-slow statement would no longer be avoided
  (`test_avoids_connection_behind_known_slow_statement` learns its cost from an idle-path
  statement). Sampling only delays
  learning a slow statement. After the Router the two clock reads cost ~20 ns each in Rust,
  so there is nothing left to win.
* **Public methods as plain `def` returning `_query()`'s coroutine** (one coroutine layer
  less): ~50 ns, but `inspect.iscoroutinefunction(pool.select_one)` would turn False and
  the documented `async def` API would change.
* **Calling `conn._fetch` from the pool**: private usage across classes (pyright strict);
  `Fetcher` gets the same effect legitimately (subclass access, like `Transaction`).
* **A list of SHARED entries, or a set of connections under the cap, to shorten the
  Python scan**: per-statement upkeep on every transition for ~30 ns per connection;
  superseded by the Rust scan (~5 ns per connection).
* **A native pool** (submission, completion and routing all in Rust): would remove most of
  the remaining ~530 ns (coroutine frame, `Fetcher` call, idle stack), but waiters,
  `acquire()`, drain and ghost handling would have to move too, or be split across the
  boundary. Not worth it for ~0.5 µs per statement today.

### What is left (Router, idle path, ~530 ns)

The `select_*` → `_query` coroutine frame (~50 ns), `isinstance(Template)`, `conn.closed`
(29 ns), the idle `popitem` and push, `Router.begin` and `finish` (~40 ns each, including
their clock reads and the hash lookup), the `Fetcher.fetch` call, the `try`, the waiter
check. On the real database the pool still measures ~1 µs over pinned at 16 tasks, more
than the ~0.5 µs the microbenchmark accounts for; the rest (more allocations per statement,
so more GC work; cache effects with real connections) needs a profiler on Linux to
attribute.

## Pipelining earlier under load

Today the pool pipelines only when no connection is idle. On Linux, one pipelined
connection did 151k ops/s at 16 tasks against 64–72k for 16 connections: pipelining
batches several responses per socket read and loop wake-up, so it roughly halves the CPU
per statement. Two prototypes (not committed; `mk_h.py`, with two prototype `Router`
methods, `best_below(cap, score)` and `expected(sql)`):

* **h2**: before taking an idle connection, join the least stalled busy one if its score is
  below twice this statement's own expected cost, at any load.
* **h2r**: the same, only once at most a quarter of `max_size` is left idle (a reserve).

A connection behind a known-slow statement scores at least that statement's cost, so a fast
statement never joins it; one behind an unexpectedly slow statement is joined only until
that statement's age passes the threshold.

Real DB, 16 tasks on a pool of 16, 7 interleaved rounds (CPU per lookup on the loop thread;
`cpu_pool2.py -v router -v h2 -v h2r --tasks 16 --rounds 7`):

| variant | CPU µs/lookup | ops/s |
|---|---:|---:|
| Router (`b18f3a4`) | 33.38 [33.20..33.50] | 21.9k |
| h2 | **29.87 [29.64..29.95]** | **25.4k** |
| h2r | 32.08 [31.88..32.58] | 23.0k |

Both prototypes were in the `bench/mixed.py` A/B below. **h2 doubles the slow
statements' p99** (29.7–38.7 ms against 21–22 ms): a slow statement's threshold (twice its
own 20 ms) admits a connection already running another slow one, so slow statements queue
behind each other. Fast-query tails were the same or better (x16 at 20k/s: p99.9 2.8 ms
against 3.1 ms). **h2r keeps slow p99 at 21.5–23.9 ms** and fast tails like the Router's,
but saves only ~1.3 µs CPU per lookup (−4%).

So yes, pipelining earlier pays (h2: −10% CPU per lookup, +16% ops/s at 16 tasks even on
this RTT-bound host; one pipelined connection is twice as cheap per statement on Linux),
but not with a threshold relative to the statement's own cost. The next thing to try is an
absolute budget: join a busy connection only if its score is below
`min(2 × own cost, B)`, with B a few point lookups, so slow statements never join anyone.
B has to be tuned on Linux: learned point-lookup costs here are RTT-dominated (~0.4 ms
against ~50 µs there). It also changes rule 1 ("idle first"), so it needs a separate decision:
**not committed.**

## `bench/mixed.py`: slow-query routing before and after

```sh
FASTPG_DSN='postgresql://fastpg:fastpg@localhost:55432/fastpg?sslmode=disable' \
  uv run yeet ./bench/mixed.py -c 8 -p 8 -p 16 -k 8 -o 'fastpg-pool*' \
  -r 5000 -r 10000 -r 20000 -t 5
```

Three interleaved repeats per variant (`mixed_ab.py`, swapping `_pool.py`; 1% of requests
`pg_sleep(0.02)`), median [min..max]. Full table with h2 / h2r: `mixed1-summary.log`.

| config | rate | variant | fast p50 | fast p99 | fast p99.9 | slow p99 |
|---|---:|---|---|---|---|---|
| x8 cap 8 | 5k | before | 420 µs [416..485] | 920 µs [832..998] | 2.1 ms [1.7..2.2] | 21.8 ms [20.9..23.7] |
| x8 cap 8 | 5k | Router | 409 µs [393..414] | 809 µs [784..841] | 1.3 ms [1.3..1.4] | 22.4 ms [21.7..24.1] |
| x8 cap 8 | 10k | before | 511 µs [496..525] | 2.5 ms [1.4..76.1] | 20.2 ms [9.5..98.8] | 23.0 ms [21.6..96.4] |
| x8 cap 8 | 10k | Router | 466 µs [435..467] | 950 µs [943..1025] | 7.8 ms [7.5..8.1] | 21.4 ms [21.3..22.7] |
| x8 cap 8 | 20k | before | 610 µs [595..715] | 20.8 ms [20.5..21.7] | 33.1 ms [33.1..35.9] | 42.1 ms [42.0..43.1] |
| x8 cap 8 | 20k | Router | 602 µs [563..614] | 20.7 ms [20.5..20.8] | 30.1 ms [29.7..35.1] | 40.9 ms [40.5..41.6] |
| x16 cap 8 | 5k | before | 444 µs [415..489] | 1.2 ms [0.8..2.8] | 3.0 ms [1.6..5.8] | 22.0 ms [21.3..24.3] |
| x16 cap 8 | 5k | Router | 413 µs [408..421] | 802 µs [783..1179] | 1.3 ms [1.2..2.4] | 22.5 ms [21.6..24.0] |
| x16 cap 8 | 10k | before | 483 µs [468..540] | 1.2 ms [1.0..2.8] | 2.5 ms [2.1..3.5] | 21.5 ms [21.3..23.0] |
| x16 cap 8 | 10k | Router | 500 µs [480..567] | 1.3 ms [1.0..48.4] | 2.9 ms [2.0..103.0] | 21.2 ms [21.1..110.9] |
| x16 cap 8 | 20k | before | 686 µs [643..692] | 1.7 ms [1.6..2.5] | 5.9 ms [3.0..7.4] | 22.2 ms [21.6..22.8] |
| x16 cap 8 | 20k | Router | 680 µs [635..807] | 1.5 ms [1.4..1.8] | 3.1 ms [2.5..3.5] | 21.7 ms [21.4..21.8] |

No regression: slow statements still take ~21–22 ms (their own 20 ms; x8 at 20k/s is
overloaded, as before), and fast-query tails are the same or lower. The 48–110 ms maxima
are single repeats with a host-wide stall (the slow statements stalled too), in either
variant.

## What needs a Linux measurement

The pool's overhead only becomes ops/s where the loop thread is the bottleneck, which
this host isn't at 16 tasks. On the Linux benchmark machine, at `1e864ae` and `b18f3a4`
(the native module changed: rebuild), several runs each:

1. `bench/run.py -s concurrent -s concurrent-pinned -d fastpg-struct -c 16` and `-c 100`.
   Here the pool's CPU over pinned fell by ~1.1–1.3 µs per statement; at Linux's ~14–16 µs
   per statement that would take 64.4k to roughly 69k ops/s at 16 tasks (an estimate).
2. Many tasks on a smaller pool, e.g. 100 tasks on 16 (`--pool-size` from `29573d3`, not
   in this base): the biggest change (scoring 3.4 → 0.8 µs per statement in the
   microbenchmark; 25.6 → 21.2 µs CPU per lookup and 38.6k → 46.0k ops/s here).
3. `bench/mixed.py` with the `notes/pool.md` commands, to confirm the tails on Linux.
4. The pipeline-earlier heuristic with an absolute budget: closed-loop ops/s at 16 tasks,
   and `mixed.py` fast and slow tails.
5. A profile (`perf`) of the pool path: on the real database the pool still costs ~1 µs
   over pinned here, about 0.5 µs more than the microbenchmark accounts for.
