# Benchmarks

fastpg is compared with [asyncpg](https://github.com/MagicStack/asyncpg),
[psycopg 3](https://www.psycopg.org/psycopg3/) (async) and
[psqlpy](https://github.com/psqlpy-python/psqlpy) by the
[Benchmarks workflow](https://github.com/RogerThomas/fastpg/actions/workflows/benchmarks.yml)
in GitHub Actions. How it works and why: [Methodology](benchmarks.md).

The numbers below come from
[run 39671855945](https://github.com/RogerThomas/fastpg/actions/runs/39671855945), on `main`
at `10a01c5`, 2026-10-21:

* a GitHub-hosted `ubuntu-24.04` runner: AMD EPYC 9V74, 4 vCPUs, 16 GB;
* PostgreSQL 18.6 on the same VM (`synchronous_commit=off`), CPython 3.14.7, uvloop;
* fastpg 0.1.0, asyncpg 0.32.0, psycopg 3.3.6, psqlpy 0.12.1;
* 3 trials of 3 s per workload and driver, interleaved; medians.

All 297 runs passed the correctness checks.

## What is measured

* **Client-side throughput** (ops/s), latency and CPU per op of the benchmark process, for
  the same SQL, data and arguments on every driver. The server's CPU isn't measured.
* **Each driver's own pool of 16 connections**, set up as its documentation recommends.
  asyncpg runs twice. `asyncpg` is its default: the pool runs a reset query every time a
  connection goes back to it, one extra round trip per query. `asyncpg-noreset` turns that
  off (a no-op `reset=`), the faster of the two. "Best other" is the fastest of the four
  non-fastpg configurations, so usually `asyncpg-noreset`.
* **Typed workloads** (`-typed`): every driver returns the same msgspec Struct. fastpg
  decodes into it in Rust (`into=Person`); the others build it from their rows
  (`Person(*record)`, `args_row(Person)`, ...). The untyped workloads take each driver's
  native rows (tuples, `Record`s, dicts).
* **Pipelining controlled for**: `fastpg-inflight1` allows one statement per connection,
  like the other pools. It is within noise of default fastpg everywhere except `point-c64`,
  the one workload with more tasks than connections, so the other leads don't come from
  pipelining.
* **Checked results**: before timing, every driver's rows are compared with the expected
  values; after a write workload, the table contents are. A mismatch fails the run.

## Throughput

Higher is better. CPU is the client's CPU time per op. "vs asyncpg" is asyncpg's defaults,
which reset the session after every pooled query: fastpg's pool queries and bulk writes don't
(they mustn't change session state), so for those rows "vs best other" is the like-for-like
column. fastpg's `pool.transaction()` does reset the session (in the COMMIT's round trip), as
asyncpg's default does and the other pools don't: for the transaction row, "vs asyncpg" is the
like-for-like one.

| workload | fastpg | best other | vs best other | vs asyncpg | CPU/op: fastpg / best other |
|---|---:|---|---:|---:|---:|
| `point-c1`: primary-key lookup, 1 task | 9,284/s | 6,310/s `asyncpg-noreset` | 1.47x | 2.65x | 39 / 85 µs |
| `point-c16`: same, 16 tasks | 38,402/s | 17,262/s `asyncpg-noreset` | 2.22x | 3.41x | 23 / 56 µs |
| `point-c64`: same, 64 tasks¹ | 54,019/s | 17,577/s `asyncpg-noreset` | 3.07x | 4.83x | 17 / 55 µs |
| `point-c16-typed` | 39,774/s | 17,144/s `asyncpg-noreset` | 2.32x | 3.60x | 23 / 57 µs |
| `page-20-c16`: 20 rows, 16 tasks | 19,270/s | 9,480/s `asyncpg-noreset` | 2.03x | 2.65x | 45 / 99 µs |
| `page-20-c16-typed` | 19,002/s | 9,094/s `asyncpg-noreset` | 2.09x | 2.70x | 48 / 106 µs |
| `fetch-1000-c4`: 1,000 rows, 4 tasks | 1.14M rows/s | 545k rows/s `asyncpg-noreset` | 2.08x | 2.17x | 0.86 / 1.82 ms |
| `fetch-1000-c4-typed` | 1.03M rows/s | 476k rows/s `asyncpg` | 2.15x | 2.15x | 0.96 / 2.09 ms |
| `wide-50-c16`: 50 rows of 13 typed columns² | 7,806/s | 4,889/s `asyncpg-noreset` | 1.60x | 1.84x | 122 / 200 µs |
| `wide-50-c16-typed` | 6,522/s | 4,668/s `asyncpg-noreset` | 1.40x | 1.65x | 146 / 212 µs |
| `insert-c16`: single-row INSERT, 16 tasks | 35,860/s | 16,087/s `asyncpg-noreset` | 2.23x | 3.38x | 23 / 60 µs |
| `update-c16`: single-row UPDATE, 16 tasks | 35,766/s | 17,718/s `asyncpg-noreset` | 2.02x | 2.99x | 22 / 52 µs |
| `transaction-c16`: 4 statements in BEGIN/COMMIT | 5,893/s | 5,205/s `asyncpg-noreset` | 1.13x | 1.27x | 138 / 175 µs |
| `executemany-1000`: 1,000-row batch | 256k rows/s | 177k rows/s `asyncpg-noreset` | 1.44x | 1.48x | 0.62 / 2.42 ms |
| `copy-5000`: binary COPY, 5,000 rows | 1.16M rows/s | 823k rows/s `psycopg` | 1.41x | 2.65x | 1.30 / 5.19 ms |

¹ With more tasks than connections, fastpg pipelines. `fastpg-inflight1` does 37,599/s
(2.14x the best other).

² int4, uuid, timestamptz, date, numeric, float8, bool, three text (one nullable), jsonb,
bytea and int8.

## Latency at a fixed request rate

Primary-key lookups arriving at 1,000 and 4,000 requests/s (Poisson), each as its own task,
latency counted from the scheduled arrival. Lower is better. *Loop busy* is the event-loop
thread's CPU share.

| | fastpg | asyncpg | asyncpg-noreset | psycopg | psqlpy |
|---|---:|---:|---:|---:|---:|
| p99 at 1,000/s | 855 µs | 1,355 µs | 961 µs | 1,071 µs | 1,458 µs |
| p99.9 at 1,000/s | 1,092 µs | 2,461 µs | 1,364 µs | 1,566 µs | 2,473 µs |
| p99 at 4,000/s | 1,034 µs | 2,881 µs | 1,270 µs | 1,368 µs | 2,980 µs |
| p99.9 at 4,000/s | 2,917 µs | 4,161 µs | 3,114 µs | 2,946 µs | 5,386 µs |
| loop busy at 4,000/s | 55% | 67% | 61% | 68% | 66% |

## Where the lead is small

* **Round-trip-bound work.** `transaction-c16` is six round trips per op. fastpg leads asyncpg,
  which resets the session after it too, by 1.27x, and the pools that don't by 1.13x (1.39x in
  the [earlier run](https://github.com/RogerThomas/fastpg/actions/runs/39667991922)).
* **Tail latency at moderate load.** At 4,000 requests/s, fastpg's p99.9 is within 7% of
  psycopg's and asyncpg-noreset's: a tie on a shared runner.
* **A real network.** Client and server share a VM, so round trips are short. Over a
  network, the round trip adds the same time to every driver, and single-query latency
  (`point-c1`) differences shrink; throughput under concurrency and CPU per op carry over.

## Caveats

* Shared CI runners are noisy. Compare drivers within a run, not runs with each other: the
  [earlier run](https://github.com/RogerThomas/fastpg/actions/runs/39667991922) (AMD EPYC
  7763) had absolute numbers 25-45% lower, and ratios within 0.3x of these.
* PostgreSQL shares the runner's 4 vCPUs with the client, and limits the concurrent
  workloads for every driver.
* One process, one event loop, a pool of 16, and small tables that fit in memory.
* The typed workloads' Structs have plain field types, so fastpg builds them directly. A
  Struct that needs msgspec validation (`Meta` constraints, enums, `Literal`) is decoded
  through MessagePack instead, which costs more ([Structs](guide/structs.md#strategies)).
* We wrote the suite. Run it on your hardware: `task db-up bench-ci`
  ([Methodology](benchmarks.md#running-it)).
