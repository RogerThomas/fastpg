# Benchmark methodology

This page explains how the [benchmarks](performance.md) are run, and why.

The aim is a fair comparison:

* every driver set up the way its own docs recommend;
* the same queries and the same data for every driver;
* every result checked;
* repeated, interleaved trials;
* every workload reported, including those where fastpg's lead is small.

The suite is `bench/compare.py`, with its code in `bench/comparison/`.
The [Benchmarks workflow](https://github.com/RogerThomas/fastpg/actions/workflows/benchmarks.yml)
runs it in GitHub Actions: on pushes to `main` that change the library or the benchmarks,
weekly, on pull requests labelled `benchmark`, and on demand.
Each run writes its results to the run's summary page, with the raw JSON as an artifact.

## Running it

```sh
task db-up bench-ci                          # what CI runs: 3 trials of 3 s
task bench-ci TRIALS=5 DURATION=10           # longer and steadier
task bench-ci -- -k 'point-*' -d fastpg -d 'asyncpg*'
uv run yeet ./bench/compare.py --list        # list workloads and drivers
```

`task bench-ci` also takes `DSN`, `WARMUP`, `OUT` (the output directory) and `SUMMARY` (a
file to append the Markdown summary to).
Anything after `--` goes to `bench/compare.py`.
A full run takes about 25 minutes.

The suite creates its own `fastpg_bench` schema and fills it with generated data.
It touches nothing else in the database.

On macOS, Docker Desktop adds a few hundred microseconds to every round trip.
Use a Linux host, or a PostgreSQL that runs natively.

## The setup in CI

* A GitHub-hosted `ubuntu-24.04` runner: AMD EPYC 9V74, 4 vCPUs, 16 GB.
* PostgreSQL 18.6 on the same machine, with `synchronous_commit=off`.
* CPython 3.14.7 and uvloop.
* fastpg 0.1.0, asyncpg 0.32.0, psycopg 3.3.6 and psqlpy 0.12.1.
* 3 trials of 3 seconds for each workload and driver. The tables show the median.

## Drivers

Every driver uses its own pool of 16 connections, in the same process, on the same event
loop.

| name | setup |
|---|---|
| `fastpg` | `fastpg.create_pool(min_size=16, max_size=16)`, defaults otherwise |
| `fastpg-inflight1` | the same with `max_inflight_per_conn=1`: one query per connection at a time, like the other pools |
| `asyncpg` | `asyncpg.create_pool(min_size=16, max_size=16)`, defaults otherwise |
| `asyncpg-noreset` | the same, with the session reset turned off (a no-op `reset=`) |
| `psycopg` | `psycopg_pool.AsyncConnectionPool(min_size=16, max_size=16, kwargs={"autocommit": True})`, with the C extension |
| `psqlpy` | `psqlpy.ConnectionPool(max_db_pool_size=16)`, defaults otherwise |

"Fastest other driver" in the results is the fastest of `asyncpg`, `asyncpg-noreset`,
`psycopg` and `psqlpy`.

Why each driver is set up this way:

* **asyncpg's session reset.**
  By default, asyncpg's pool runs a reset query each time a connection goes back to the
  pool.
  That costs one extra round trip per query.
  fastpg's pool doesn't reset after single queries, and neither do psycopg's and psqlpy's.
  So `asyncpg-noreset` is the like-for-like comparison.
  It's safe here because no workload changes session state.
  fastpg's `pool.transaction()` does reset, inside the `COMMIT`'s round trip, so on the
  transaction workload default `asyncpg` is like for like: there fastpg leads it by 1.27x.
* **Queueing on busy connections (fastpg).**
  fastpg's pool sends a query to an idle connection first.
  Only when all are busy does it send several queries to one connection.
  With 16 tasks on 16 connections there's always an idle one, so this never happens.
  With 64 tasks it does, so `fastpg-inflight1` shows fastpg without it.
* **Autocommit.**
  Single queries run in autocommit on every driver.
  psycopg needs `autocommit=True` for this; otherwise it wraps each query in a transaction,
  which costs two extra round trips.
* **Prepared statements.**
  Every driver runs prepared statements on the server.
  psycopg prepares a statement after 5 runs; the warm-up runs each one far more often.
* **Text or binary results.**
  asyncpg, fastpg and psqlpy always use binary.
  psycopg's default text format was as fast or faster than binary here, so it keeps it.
* **JSON.**
  asyncpg and psycopg are given `msgspec.json.decode` to parse `jsonb`, the fastest
  decoder they allow. psqlpy and fastpg parse it natively.
* **Bulk writes.**
  `executemany` is all or nothing on every driver.
  COPY is binary `COPY ... FROM STDIN` on every driver.
  Every driver gets the same list of tuples.
* **Nothing else is tuned.**

## Row types

The read workloads run twice:

* **Native rows**: each driver's own row type (fastpg and psycopg tuples, asyncpg `Record`s,
  psqlpy dicts). This is the least work each driver can do.
* **Structs** (workloads ending in `-typed`): every driver returns the same msgspec Struct.
  fastpg uses `into=Person`.
  asyncpg uses `Person(*record)`, psycopg `row_factory=args_row(Person)`, and psqlpy
  `Person(*row)` on its tuple rows.
  This is what an app that wants typed objects really pays.

## Checking results

Before a read workload is timed, each driver's rows are compared with the expected values.
After a write workload, the table contents are checked.
A wrong result fails the run, and the CI job fails with it.

## Workloads

| workload | one operation | tasks |
|---|---|---:|
| `point-c1` | one `people` row by primary key | 1 |
| `point-c16` | the same | 16 |
| `point-c64` | the same, with more tasks than connections | 64 |
| `point-c16-typed` | the same, into a Struct | 16 |
| `page-20-c16`(`-typed`) | 20 `people` rows | 16 |
| `fetch-1000-c4`(`-typed`) | 1,000 `people` rows | 4 |
| `wide-50-c16`(`-typed`) | 50 rows of 13 columns of costlier types (uuid, timestamptz, numeric, jsonb, ...) | 16 |
| `insert-c16` | `INSERT` one row | 16 |
| `update-c16` | `UPDATE` one row by primary key | 16 |
| `transaction-c16` | `BEGIN`, lock two rows, two `UPDATE`s, one `INSERT`, `COMMIT` | 16 |
| `executemany-1000` | a 1,000-row `INSERT` batch | 1 |
| `copy-5000` | 5,000 rows by binary COPY | 1 |
| `open-loop-1000`, `open-loop-4000` | lookups arriving at a steady rate | – |

Bulk loads write to an unlogged table, so the runner's disk isn't part of the measurement.
No workload reads or sends arrays.

## How the numbers are computed

* **Trials.** Each trial runs every workload. Within a workload, the drivers run in a
  shuffled order, so drift over the run doesn't always hit the same driver.
* **Each run** opens a fresh pool, checks results, warms up, then measures.
  Before each run, the write tables are emptied and the server runs a `CHECKPOINT`.
* **Throughput** is operations per second of the measured phase. Tables show the median of
  the trials.
* **Latency** is measured around each `await`. Tables show the median of the trials.
* **CPU per operation** is the benchmark process's CPU time, all threads included, divided
  by the operations. The server's CPU isn't counted.
* **Steady-rate latency.** Requests arrive at random times, at a fixed average rate,
  whether or not earlier ones have finished. Latency counts from when a request was due,
  so a driver that keeps the event loop busy pays for the delay it causes.

## Limits

* **Shared, noisy machines.** The runners are shared VMs, with PostgreSQL on the same
  machine. Compare drivers within one run, not across runs. Treat a few percent as a tie.
  An [earlier run](https://github.com/RogerThomas/fastpg/actions/runs/39667991922), on a
  different CPU, was 25-45% slower overall, with ratios within 0.3x of these.
* **No network.** Client and server talk over loopback, so round trips are short.
  Over a real network, the round trip adds the same time to every driver.
* **Client-side only.** The server's CPU isn't measured.
* **One shape.** One process, one event loop, a pool of 16, and small tables that fit in
  memory.
* **Structs with plain fields.** A Struct that needs msgspec validation is slower to build
  (see [Structs](guide/structs.md#validation)).
* **We wrote the suite.** Run it on your own hardware, with your own queries.
  Pull requests that make another driver faster here are welcome.

## Full results

From [run 39671855945](https://github.com/RogerThomas/fastpg/actions/runs/39671855945).
"vs default asyncpg" compares with asyncpg's default pool, which resets the session after
every query.
CPU is the client's CPU time per operation.

| workload | fastpg | fastest other | vs fastest other | vs default asyncpg | CPU/op: fastpg / fastest other |
|---|---:|---|---:|---:|---:|
| `point-c1` | 9,284/s | 6,310/s `asyncpg-noreset` | 1.47x | 2.65x | 39 / 85 µs |
| `point-c16` | 38,402/s | 17,262/s `asyncpg-noreset` | 2.22x | 3.41x | 23 / 56 µs |
| `point-c64` | 54,019/s | 17,577/s `asyncpg-noreset` | 3.07x | 4.83x | 17 / 55 µs |
| `point-c16-typed` | 39,774/s | 17,144/s `asyncpg-noreset` | 2.32x | 3.60x | 23 / 57 µs |
| `page-20-c16` | 19,270/s | 9,480/s `asyncpg-noreset` | 2.03x | 2.65x | 45 / 99 µs |
| `page-20-c16-typed` | 19,002/s | 9,094/s `asyncpg-noreset` | 2.09x | 2.70x | 48 / 106 µs |
| `fetch-1000-c4` | 1.14M rows/s | 545k rows/s `asyncpg-noreset` | 2.08x | 2.17x | 0.86 / 1.82 ms |
| `fetch-1000-c4-typed` | 1.03M rows/s | 476k rows/s `asyncpg` | 2.15x | 2.15x | 0.96 / 2.09 ms |
| `wide-50-c16` | 7,806/s | 4,889/s `asyncpg-noreset` | 1.60x | 1.84x | 122 / 200 µs |
| `wide-50-c16-typed` | 6,522/s | 4,668/s `asyncpg-noreset` | 1.40x | 1.65x | 146 / 212 µs |
| `insert-c16` | 35,860/s | 16,087/s `asyncpg-noreset` | 2.23x | 3.38x | 23 / 60 µs |
| `update-c16` | 35,766/s | 17,718/s `asyncpg-noreset` | 2.02x | 2.99x | 22 / 52 µs |
| `transaction-c16` | 5,893/s | 5,205/s `asyncpg-noreset` | 1.13x | 1.27x | 138 / 175 µs |
| `executemany-1000` | 256k rows/s | 177k rows/s `asyncpg-noreset` | 1.44x | 1.48x | 0.62 / 2.42 ms |
| `copy-5000` | 1.16M rows/s | 823k rows/s `psycopg` | 1.41x | 2.65x | 1.30 / 5.19 ms |

Latency at a steady rate, with every configuration:

| | fastpg | asyncpg | asyncpg-noreset | psycopg | psqlpy |
|---|---:|---:|---:|---:|---:|
| p99 at 1,000/s | 855 µs | 1,355 µs | 961 µs | 1,071 µs | 1,458 µs |
| p99.9 at 1,000/s | 1,092 µs | 2,461 µs | 1,364 µs | 1,566 µs | 2,473 µs |
| p99 at 4,000/s | 1,034 µs | 2,881 µs | 1,270 µs | 1,368 µs | 2,980 µs |
| p99.9 at 4,000/s | 2,917 µs | 4,161 µs | 3,114 µs | 2,946 µs | 5,386 µs |
| event loop busy at 4,000/s | 55% | 67% | 61% | 68% | 66% |

All 297 runs passed the result checks.
