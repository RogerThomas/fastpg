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
To test a released wheel, installed from PyPI:
`gh workflow run benchmarks.yml -f fastpg-version=0.2.1`.
Other runs build fastpg from source, without the release wheels' profile-guided optimisation.

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

## Running it on AWS

`task bench-aws` runs `task bench-ci` for a fastpg release on AWS EC2 spot instances:
x86_64 on an m8a.2xlarge and arm64 on an m9g.2xlarge, with PostgreSQL on the same machine,
as in CI.
The workloads are split into pieces that run in parallel, one instance each, and the results
are merged into one report per architecture under `bench/results/aws-<run id>/`.
Every instance it starts is terminated at the end, on Ctrl-C too.
The published results come from it.

```sh
task bench-aws -- --fastpg-version 0.2.1
```

It needs the aws CLI with the `fastpg-benchmarks` profile.

## The setup

* An x86_64 AWS m8a.2xlarge, 8 vCPU.
  An arm64 AWS m9g.2xlarge, also 8 vCPU, gives similar ratios.
* PostgreSQL 18.6 in Docker on the same machine, with `synchronous_commit=off`.
* CPython 3.14.7 and uvloop.
* fastpg 0.2.1, asyncpg 0.32.0, psycopg 3.3.6 and psqlpy 0.13.1.
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
  transaction workload default `asyncpg` is like for like: there fastpg leads it by 1.58x.
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

* **Spot instances.** Each piece of a run gets its own 8-vCPU spot instance, which still
  varies a little from run to run. Read the ratios, and treat a few percent as a tie.
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

From the x86_64 AWS run of fastpg 0.2.1 on 2026-11-10.
"vs default asyncpg" compares with asyncpg's default pool, which resets the session after
every query.
CPU is the client's CPU time per operation.

| workload | fastpg | fastest other | vs fastest other | vs default asyncpg | CPU/op: fastpg / fastest other |
|---|---:|---|---:|---:|---:|
| `point-c1` | 49,994/s | 35,562/s `asyncpg-noreset` | 1.41x | 2.41x | 7.7 / 15.9 µs |
| `point-c16` | 154,049/s | 69,483/s `asyncpg-noreset` | 2.22x | 3.06x | 6.4 / 14.4 µs |
| `point-c64` | 188,227/s | 63,340/s `asyncpg-noreset` | 2.97x | 4.05x | 5.3 / 15.8 µs |
| `point-c16-typed` | 149,250/s | 68,617/s `asyncpg-noreset` | 2.18x | 3.00x | 6.7 / 14.6 µs |
| `page-20-c16` | 72,070/s | 28,748/s `asyncpg-noreset` | 2.51x | 2.96x | 13.8 / 34.8 µs |
| `page-20-c16-typed` | 66,242/s | 26,936/s `asyncpg-noreset` | 2.46x | 2.87x | 15.0 / 37.1 µs |
| `fetch-1000-c4` | 2.57M rows/s | 884k rows/s `psycopg` | 2.91x | 3.02x | 0.39 / 1.13 ms |
| `fetch-1000-c4-typed` | 2.31M rows/s | 823k rows/s `psycopg` | 2.81x | 3.01x | 0.43 / 1.22 ms |
| `wide-50-c16` | 17,168/s | 10,600/s `asyncpg-noreset` | 1.62x | 1.74x | 58.1 / 94.3 µs |
| `wide-50-c16-typed` | 14,884/s | 10,044/s `asyncpg-noreset` | 1.48x | 1.60x | 67.1 / 99.6 µs |
| `insert-c16` | 153,807/s | 67,990/s `asyncpg-noreset` | 2.26x | 3.16x | 6.5 / 14.7 µs |
| `update-c16` | 167,282/s | 75,569/s `asyncpg-noreset` | 2.21x | 3.15x | 5.8 / 13.2 µs |
| `transaction-c16` | 29,204/s | 20,489/s `asyncpg-noreset` | 1.43x | 1.58x | 33.2 / 48.8 µs |
| `executemany-1000` | 407k rows/s | 254k rows/s `asyncpg` | 1.60x | 1.60x | 0.25 / 1.72 ms |
| `copy-5000` | 1.37M rows/s | 1.11M rows/s `psycopg` | 1.23x | 2.40x | 0.52 / 3.31 ms |

Latency at a steady rate, with every configuration:

| | fastpg | asyncpg | asyncpg-noreset | psycopg | psqlpy |
|---|---:|---:|---:|---:|---:|
| p99 at 1,000/s | 617 µs | 702 µs | 636 µs | 678 µs | 747 µs |
| p99.9 at 1,000/s | 652 µs | 787 µs | 672 µs | 732 µs | 807 µs |
| p99 at 4,000/s | 598 µs | 688 µs | 633 µs | 656 µs | 817 µs |
| p99.9 at 4,000/s | 624 µs | 759 µs | 690 µs | 713 µs | 961 µs |
| event loop busy at 4,000/s | 51% | 52% | 52% | 54% | 52% |

All 297 runs passed the result checks.
