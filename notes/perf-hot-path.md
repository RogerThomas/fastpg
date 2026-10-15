# The point-lookup hot path: where the µs go, and what was cut

Branch work on the single-query path: `await conn.select_one(SQL, 42, into=Person)` with
the 10-column `people` row. Code: `native/src/event_loop.rs` (`LoopSocket::read`),
`native/src/rows.rs`, `native/src/structs.rs` (`StructBuilder`), `native/src/connection.rs`
(`resolve`, `Shared::pump` / `flush`), `src/fastpg/_connection.py` (`_Queries._fetch`,
`connect`). Test: `tests/test_connection.py::test_queries_sent_after_results_arrive_together`.
Scripts, commands and raw logs: [notes/eval/2026-10-hot-path/](eval/2026-10-hot-path/README.md).

**Everything here was measured on the macOS host** (M3 Max, PostgreSQL in Docker Desktop
behind its port forward), shared with other benchmarks (load average 5–13), so
comparisons are interleaved A/B runs of two builds with medians and spreads. Linux numbers
are needed to confirm the absolute gains; see the last section.

## Summary

| commit | change | effect (macOS host) |
|---|---|---|
| `17d0e70` | skip the empty `recv` after a short read | **4 → 3 syscalls** per query (same as asyncpg); ~0.2 µs here, more on Linux (est.) |
| `b5e4bae` | reserve each row's bytes and cells once | **22 → 16** Rust allocations per query; −0.2 µs user CPU (pipelined) |
| `63b0e0a` | no per-query Struct argument buffers (≤ 16 columns) | 16 → **14** allocations; not measurable |
| `a6b1faa` | `_fetch` returns the native future (one coroutine less) | Python side of `select_one` 456 → **398 ns** |
| `fedb81e` | no `future.done()` call per query | one Python call less (~40 ns); not measurable end to end |
| `7114f07` | **write coalescing**: queries of tasks woken together go out in one `send` | pipelined, 32 tasks: `send`/query 1.00 → **0.28**, CPU/query 24.3 → **12.8 µs**; sequential unchanged |

All six together, base (`1e864ae`) vs branch head, interleaved (`final.log`):

| scenario | metric | before | after |
|---|---|---:|---:|
| sequential, `hot.py` (10 pairs × 3 rounds × 4,000) | process CPU µs/query, median (IQR) | 46.2 (42.9–49.4) | 44.2 (41.7–47.6) |
| | paired delta, median (IQR) | | −2.5 (−5.2 to +4.6): within noise |
| sequential, `bench/cpu_per_request.py` (6 pairs × 5,000) | process CPU µs/req, median (min–max) | 45.5 (41.9–50.4) | 43.7 (41.2–51.0) |
| 16 tasks, one connection, `hot.py` | process CPU µs/query | 26.6 (26.1–27.4) | **13.3** (13.0–14.7) |
| | user CPU µs/query | 5.71 (5.60–5.78) | **4.75** (4.69–4.82) |
| `cpu_per_request.py -t 16 -d fastpg-pipelined` (4 pairs) | µs/req; req/s | 26.4; 21.0k | **13.7; 28.0k** |
| `cpu_per_request.py -t 16` (pool of 1) | µs/req; req/s | 38.0; 13.0k | **35.8; 13.6k** |
| any point lookup | syscalls | 4 | **3** |
| Struct / tuple point lookup | Rust allocations | 22 / 19 | **14 / 13** |

The sequential gain is below what this host can resolve end to end (expected ~0.5 µs here:
see the per-commit numbers). Commits 1–5 without the write coalescing, on 16 pipelined tasks
where user time is stable: process CPU −0.71 µs (IQR −1.28 to −0.43), user −0.31 µs (IQR
−0.48 to −0.28) per query (`pipe16-base-vs-1to5.log`).

## Method

* **CPU per query**: `resource.getrusage` user and system time around N sequential (or
  pipelined) lookups, the same quantity as `bench/cpu_per_request.py`'s process CPU, split
  into user and kernel time. `ab.py` alternates two builds (A B, B A, ...) in fresh
  processes, 3 rounds each, and reports medians, IQRs and the paired difference.
* **Builds** are copies of `src/fastpg` (Python + extension) put first on `PYTHONPATH`, so
  one venv serves every variant.
* **Syscalls**: a `DYLD_INSERT_LIBRARIES` interposer (`syscount.c`) counting `recv`, `send`,
  `kevent`, ... (dtrace needs root).
* **Allocations**: the `alloc-stats` feature (`_native.rust_allocations()`).
* **Profile**: `sample <pid> 60 1` (1 ms stack samples) of a sequential run, summarised per
  call path by `sagg.py`. Samples in `kevent` are mostly blocked time and are left out.
* **Python side**: `pyover.py` runs `Connection.select_one` against a stub native object
  that resolves the future at once: no I/O, very stable (±2%).

Noise: an A/A run (the same build twice) gave a paired CPU delta of −1.2 µs median, IQR −8.2
to +1.4, on sequential lookups. The host occasionally ran a whole process at roughly half
speed (efficiency cores or contention), hence the wide upper IQRs. Sequential effects below
~1 µs are not resolvable end to end here; they were measured with targeted counters (syscalls,
allocations) or microbenchmarks instead, and pipelined runs (user time is stable to ±0.1 µs
there) were used for user-space changes.

## Where the µs go (before)

### Syscalls per query (sequential, uvloop)

| | `recv` | `send` | `kevent` | total |
|---|---:|---:|---:|---:|
| fastpg before | 2.00 (one returns `EAGAIN`) | 1.00 | 1.00 | 4 |
| fastpg after (`17d0e70`) | **1.00** | 1.00 | 1.00 | **3** |
| asyncpg | 1.00 | 1.00 | 1.00 | 3 |

With the stdlib asyncio loop both drivers make 2 `kevent` calls per query.

xitca's driver reads until `WouldBlock`, so every response cost a second, empty `recv`.
`send` and the flush are already coalesced: Bind/Execute/Sync go out in one `send`
(`flush` is a no-op on the plain socket). Three syscalls is the floor for a request /
response on one socket under asyncio (write, wait, read).

### Process CPU per query, sequential (macOS)

User ≈ 9–12 µs, system ≈ 25–31 µs (process CPU 35–45 µs depending on host load). asyncpg
in the same harness, run back to back during a busy period: user 34.7 vs fastpg's 22.9 µs,
system 69.2 vs 66.8 µs (medians of 7 rounds): the user-space gap is fastpg's advantage, the
kernel side is the same three-or-four syscalls.

On this host the kernel side dominates: a loopback `send` or a `recv` with data is several
µs because the receiving side's TCP processing runs in the caller's syscall, while an
`EAGAIN` `recv` costs ~210 ns (`sysbench.c`).

### Profile (60 s `sample`, ~134k sequential queries, base + `17d0e70`)

From `sample-breakdown.txt` (1 sample = 1 ms; µs per query = samples / 134):

| where | samples | µs/query |
|---|---:|---:|
| `send` (inside `fetch` → pump → driver write) | 1,179 | 8.8 |
| `recv` (inside `on_readable` → pump → driver read) | 1,218 | 9.1 |
| **Rust, submit** (`fetch` minus `send`): params, cache lookup, spawn, xitca Bind/Execute/Sync encode, executor | 156 | 1.2 |
| **Rust, read path** (`on_readable` minus `recv` and drain): xitca message routing, response channel, row copy, executor | 383 | 2.9 |
| — of which `RowBuffer` reallocations (fixed in `b5e4bae`) | 87 | 0.65 |
| — of which xitca's boxed `ready()` future per readiness wait (alloc + free) | 71 | 0.5 |
| — of which the per-request tokio channel's drop (xitca) | 30 | 0.2 |
| **Rust, drain**: decode + resolve | 332 | 2.5 |
| — decode 10 cells + `Person(...)` vectorcall (of which `Decimal(str)` 56) | 163 | 1.2 |
| — `future.set_result` → `loop.call_soon` (uvloop Handle) | 96 | 0.7 |
| — `future.done()` (removed in `fedb81e`) | 17 | 0.13 |
| **Python**: task step minus `fetch` (coroutine frames, `create_future` 35, the `StopIteration` that ends `await future` 16–23, plan lookup, the benchmark loop) | 306 | 2.3 |
| **uvloop and the rest**: poll dispatch, idle handler, Handle / Future deallocation | 311 | 2.3 |
| total on-CPU (excluding `kevent`) | 3,885 | 29.0 |

So roughly: kernel 18 µs + `kevent`; work under the native calls 6.6 µs (including the
Python objects decode creates and `set_result`'s `call_soon`; ~1.5 µs of it is xitca's
per-query allocations and channels); Python and uvloop 4.6 µs, most of it the asyncio
machinery every driver pays (future creation and resolution, task wake-up via `call_soon`,
coroutine resumption). Rust allocations per point lookup were 22 (19 for tuples): ours were the
parameter vector, the request, the task box and its waker, the row buffer's growth (≈8),
the Struct builder's three vectors; xitca's are the response channel (2, plus a lazily
boxed pthread mutex on macOS), one boxed `ready()` future per readiness wait (≈3) and the
row's range vector.

The same profile of the branch head (`sample-breakdown-final.txt`, ~146k queries) has no
`RowBuffer` growth or `done()` frames left, and gives Rust submit 1.1, read path 2.2,
drain 1.7 and Python 1.7 µs per query. Two profiles taken minutes apart on this host differ
in CPU speed as much as in code, so treat that as indicative; the interleaved A/B numbers
are the measurements.

**On Linux** (the 26–28 µs figure) the split is unknown; user time should be close to this
host's (same CPU, in a VM), which would put the kernel share at roughly 15–18 µs for the
same 3–4 syscalls through a veth bridge.

## Candidates, with numbers

### 1. Skip the empty read after a short read (`17d0e70`) — done

`LoopSocket::read` treats a read that didn't fill the buffer as having drained the socket:
the next read returns `WouldBlock` without a syscall (and clears `readable`). Safe because
the loop's readiness is level-triggered (and a short read means no data was left, so even
an edge would fire for newer data): anything arriving later, EOF included, produces another
`on_readable`, which sets `readable` again. The one behaviour change: data and FIN arriving
together used to be noticed in one pump (the second read returned 0); now EOF is noticed one
loop iteration later.

* Syscalls per query: 2 `recv` → 1 (`sysc.py`; tasks=32: 0.21 `recv` per query).
* An `EAGAIN` `recv` costs ~210 ns here (`sysbench.c`), so the gain is below this host's
  end-to-end noise (interleaved sequential A/B: CPU delta +1.2 µs median, IQR −0.9 to
  +15.6: inconclusive). On Linux an `EAGAIN` `recv` on TCP (syscall entry/exit, socket
  lock, in a VM) should be ~0.3–1 µs: **needs a Linux measurement**.

### 2. Reserve each row once (`b5e4bae`) — done

`RowBuffer::push_row` grew the arena and the cell index cell by cell (about six
reallocations for this row). It now sums the cell lengths first and reserves both once
(amortised for large results).

* Rust allocations per point lookup: Struct 22 → 16, tuple 19 → 13 (`SELECT 1`: 12 → 12).
* Pipelined (32 tasks, user CPU, 24 rounds each, interleaved): paired delta −0.21 µs
  median, IQR −0.25 to −0.05. Sequential: not resolvable (−0.14 µs median, IQR −1.1 to +2.1).

### 3. Struct builder without per-query buffers (`63b0e0a`) — done, marginal

The JSON decoders are read from the spec instead of cloned into a per-query vector, and rows
of up to 16 columns pass their vectorcall arguments from a stack array.

* Allocations: 16 → 14 per Struct point lookup.
* Pipelined user CPU: −0.03 µs median (IQR −0.14 to +0.10): not measurable.

### 4. One coroutine less per query (`a6b1faa`) — done

`select_one` awaited `_fetch`, a coroutine awaiting the native future. `_fetch` is now a
plain method returning the future itself (tuple and direct routes) or a small decoding
coroutine (msgpack / json routes).

* `pyab.py` (stub native connection, 5 interleaved runs of 5 × 100k calls): 456 ns → 398 ns
  per `select_one` (min 445 → 394). The real path also saves one frame on the suspend and
  resume, which the stub doesn't exercise.

### 5. No `done()` call per query (`fedb81e`) — done, marginal

`resolve` called `future.done()` before every `set_result`. Final outcomes now go straight to
`set_result` / `set_exception`; on a cancelled awaiter's future they raise
`InvalidStateError`, which was already ignored (the cost moves to the rare cancelled query).
Streamed chunks keep the check so a cancelled large result isn't decoded.

* One Python method call less (~17 samples ≈ 0.13 µs on this host's profile; ~40 ns as a
  microbenchmark). Pipelined A/B: −0.06 µs user median, within noise.

### 6. Write coalescing for pipelined queries (`7114f07`) — done, needs Linux confirmation

Not the sequential path, but the biggest measured win, so it's here. Every query was written
as soon as it was submitted. When one read resolves several results, their tasks run back
to back in the next loop iteration and each wrote its next query with its own `send`, which
with N tasks on one connection meant one `send` per query while reads were already batched.

Now a pump that resolves more than one result schedules `flush()` with `loop.call_soon`,
behind the task wake-ups it just queued; queries submitted until the flush runs only queue
their executor task (`flush_pending`), and the flush polls them so the driver writes them in
one `send`. A pump that resolves a single result (sequential use) schedules nothing, so the
sequential path is unchanged. The new test blocks the loop once so that 16 results are read
together, then checks every task's second query (half of them failing) comes back right; with
the flush disabled it times out (mutation-checked).

`sysc.py --tasks T` (`send` per query, before → after): T=2 1.00 → 0.52, T=4 1.00 → 0.49,
T=16 1.00 → 0.36, T=32 1.00 → 0.28; sequential 1.00 → 1.00.

`ab.py`, interleaved, process CPU µs per query (median, IQR), the previous commit
(`fedb81e`) → this one; 5–6 pairs × 3 rounds:

| scenario | before | after | paired delta | ops/s |
|---|---:|---:|---:|---:|
| one connection, 32 tasks | 24.26 (23.75–24.58) | **12.79** (12.74–12.92) | −11.42 | |
| one connection, 16 tasks | 27.11 (26.50–27.91) | **16.04** (15.67–16.19) | −11.26 | 27.5k → 32.4k |
| one connection, 2 tasks | 29.66 (29.01–30.77) | **23.15** (22.48–23.68) | −6.90 | 5.2k → 5.4k |
| pool of 1, 16 tasks | 36.55 (35.47–37.21) | **33.34** (32.81–33.91) | −3.15 | 16.0k → 17.1k |
| pool of 4, 16 tasks | 32.10 (30.41–35.81) | **29.24** (28.87–30.18) | −2.45 | 17.2k → 17.7k |
| pool of 16, 16 tasks | 39.30 (39.07–39.70) | 39.34 (38.01–40.01) | −0.20 | unchanged |
| sequential | 38.64 (36.18–40.55) | 37.88 (36.80–38.61) | −0.35 | unchanged |

(Wall-clock throughput here is bound by Docker Desktop's ~300 µs port-forward round trip,
so the CPU column is the meaningful one.) The pool gains less: a task woken on one
connection usually sends its next query through another, and the pool's own FIFO hand-off
wakes waiters one iteration after the flush.

Trade-off: the first query of a batch now waits for the other woken tasks' Python work
before it is written (previously it went out as soon as its task ran). The batch's queries
reach PostgreSQL together and the loop spends less time in `send`, so the batch as a whole
finishes sooner, but the first query's server-side start is later by up to the other tasks'
run time. Measure open-loop p99 (`bench/open_loop.py`) and `bench/mixed.py` on Linux before
relying on it; the threshold (more than one result per pump) is the knob.

## Ranking

By expected gain on the brief's target (sequential point lookup on Linux), then risk and
complexity:

| rank | change | gain | risk | complexity |
|---:|---|---|---|---|
| 1 | short read (`17d0e70`) | −1 of 4 syscalls on every response; ~0.3–1 µs on Linux (est.) | low: relies on the loop's readiness (documented in the code); EOF seen one iteration later in one edge case | 15 lines |
| 2 | row reserve (`b5e4bae`) | −6 allocations/query; ~0.2 µs | very low | 10 lines |
| 3 | one coroutine less (`a6b1faa`) | ~0.06–0.1 µs | very low | small |
| 4 | no `done()` (`fedb81e`) | ~0.04 µs | low (cancelled futures now raise and swallow `InvalidStateError`) | tiny |
| 5 | Struct buffers (`63b0e0a`) | −2 allocations; unmeasurable | low | small |
| — | write coalescing (`7114f07`) | none sequentially; **−11 µs/query (−47%) at 32 pipelined tasks**, −3 µs through a 1-connection pool | medium: scheduling change, latency trade-off above, a lost flush would hang (tested) | ~70 lines |

For the sequential target, expect roughly 0.6–1.4 µs (2–5%) on Linux from 1–5 together,
most of it from the syscall. Write coalescing is the one with a large effect, on pipelined
and pool-of-few-connections workloads.

## Looked at, not done

* **Bypassing the asyncio future** (a Rust awaitable, or waking the task synchronously from
  `on_readable`): `set_result` → `call_soon` → task wake-up is ~0.7 µs plus the Python
  resume, but every alternative breaks asyncio's scheduling guarantees or re-enters the
  connection from inside its own callback. asyncpg pays the same. Rejected.
* **Speculative read after the write / busy-polling**: on a LAN the response is ≥ 50 µs away,
  so a read right after the write is a wasted syscall; spinning was rejected in [ADR 0058](../adr/0058-delete-spin-busy-poll.md).
* **`Decimal` via a max-precision `Context.create_decimal`**: 84 → 72 ns per numeric cell
  (timeit), with a precision/semantics argument to make. Not worth it.
* **Executor: one allocation per task** (task box and waker `Arc` merged, as tokio does):
  −1 allocation per query, a rewrite of `event_loop.rs`'s slab. Small gain; not now.
* **Cheaper dead-connection race** (`unless_dead`: tokio `watch` + `Notify` registration,
  ~0.15 µs in the profile): it is the hang fix of [ADR 0052](../adr/0052-fail-pipelined-queries-after-fatal-error.md); not worth the risk for the
  gain.
* **Statement-cache key hashing** (SipHash of the ~130-byte SQL text, ~50 ns): could reuse
  Python's cached `str` hash; small gain, API churn.
* **`Planner.plan_for`** (classmethod binding + `isinstance` + dict lookup: 54 ns vs 12 ns
  for the bare lookup): ~40 ns; skipped.
* **xitca's per-query costs** (no vendoring, [ADR 0008](../adr/0008-raw-row-access-via-fromsql.md), [ADR 0048](../adr/0048-bulk-writes.md)): a boxed future per
  `AsyncIoDyn::ready` call (≈3 per query, ~0.5 µs here), a tokio mpsc channel per request
  (`Arc<Chan>` + block allocation; plus a lazily boxed pthread mutex on macOS), a range
  vector per row stream. Worth raising upstream: an `AsyncIoDyn::poll_ready` path without
  boxing, and a reusable per-connection response queue.

Seen in passing (outside this brief; the first draft of the coalescing test only passed with
the flush disabled because of the extra traffic this causes): 16 tasks that run the same uncached SQL at once each prepare it (16 Parse round
trips; 15 statements are then closed), because `submit` checks the cache before the first
prepare has completed.

## Needs a Linux measurement

* Sequential CPU per request before/after (`bench/cpu_per_request.py`, or `cprab.py` with two
  builds), to put a number on the short read (`EAGAIN` `recv` cost in the VM) and the
  allocation savings.
* Write coalescing: `cpu_per_request.py -t 16 -d fastpg-pipelined`, the pool rows of
  `bench/run.py`'s concurrent scenario, **open-loop p99** (`bench/open_loop.py`) and
  `bench/mixed.py` for the latency trade-off, with and without CPU quotas.
* TLS: the short read changes the socket under rustls; `tests/test_tls.py` was skipped here
  (no TLS server running) and should be run (`task db-tls-up test-tls`).
