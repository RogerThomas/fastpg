# Benchmark methodology

How fastpg is compared with [asyncpg](https://github.com/MagicStack/asyncpg),
[psycopg 3](https://www.psycopg.org/psycopg3/) (async) and
[psqlpy](https://github.com/psqlpy-python/psqlpy), and why the comparison is built the way it
is. The suite is `bench/compare.py` (code in `bench/comparison/`); the
[Benchmarks workflow](https://github.com/RogerThomas/fastpg/actions/workflows/benchmarks.yml)
runs it weekly and on demand, and writes the results to the run's summary page, with the raw
JSON as an artifact.

The goal is a comparison a skeptic can't dismiss: every driver set up the way its own
documentation recommends, the same work and the same data for all of them, checked results,
repeated interleaved trials, and the workloads where fastpg doesn't win reported like the
others.

## Running it

```sh
task db-up bench-ci                          # exactly what CI runs (3 trials x 3 s)
task bench-ci TRIALS=5 DURATION=10           # longer, steadier
task bench-ci -- -k 'point-*' -d fastpg -d 'asyncpg*'
uv run yeet ./bench/compare.py --list        # workloads and driver configurations
```

`task bench-ci` takes `DSN` (default: the `task db-up` database), `TRIALS`, `DURATION`,
`WARMUP`, `OUT` (where `compare-<timestamp>.json` and `.md` go) and `SUMMARY` (a file to
append the Markdown to); anything after `--` goes to `bench/compare.py`. In GitHub, run
*Actions → Benchmarks → Run workflow*; it takes the same knobs. With the defaults a run is 99
measurements per trial, about 25 minutes, plus the build.

The suite creates its own `fastpg_bench` schema (dropped and recreated at the start of a
run; `--keep-schema` reuses it) and seeds it from closed-form formulas, so it needs an empty
database and touches nothing else. On macOS, Docker Desktop's port forwarding adds a few
hundred µs to every round trip (see `perf-notes.md`): use a Linux host or a native server.

## Driver configurations

Each configuration is one column of the results. All of them use the driver's own pool,
with the same size (16 connections, `min = max` where the pool has a minimum), the same
concurrency, the same SQL and the same argument streams, in the same process on the same
uvloop event loop.

| name | setup |
|---|---|
| `fastpg` | `fastpg.create_pool(min_size=16, max_size=16)`, defaults otherwise: binary protocol, cached prepared statements, `max_inflight_per_conn=8`. |
| `fastpg-inflight1` | The same with `max_inflight_per_conn=1`: never more than one statement in flight per connection, like every other pool here. |
| `asyncpg` | `asyncpg.create_pool(min_size=16, max_size=16)`, defaults otherwise: binary protocol, statement cache, reset query on release. |
| `asyncpg-noreset` | The same with `reset=` a no-op coroutine (asyncpg's documented hook), which skips the reset round trip. |
| `psycopg` | `psycopg_pool.AsyncConnectionPool(min_size=16, max_size=16, kwargs={"autocommit": True})`, the C implementation (`psycopg[binary]`), default text results, server-side prepared statements after 5 executions. |
| `psqlpy` | `psqlpy.ConnectionPool(max_db_pool_size=16)`, defaults otherwise: binary protocol, prepared statements. |

The choices that make these each driver's best, and where the drivers genuinely differ:

* **Pipelining (fastpg).** A fastpg pool sends a statement to an idle connection first; only
  when every connection is busy does it pipeline up to `max_inflight_per_conn` statements on
  one. With no more tasks than connections (every workload except `point-c64` and the open
  loop) there's always an idle connection, so it never pipelines (sampling `Pool.stats()`
  during 16 tasks on 16 connections found no connection with two statements in flight;
  with 64 tasks, every sample did); `fastpg-inflight1` shows those two without it, so no
  result rests on pipelining alone. (The suite never
  uses a single shared pipelined connection, which is faster still.)
* **asyncpg's reset.** asyncpg's pool runs `SELECT pg_advisory_unlock_all(); CLOSE ALL;
  UNLISTEN *; RESET ALL` every time a connection is released: one more round trip per
  pooled query. That's its default, so `asyncpg` keeps it, and `asyncpg-noreset` removes it,
  which is safe here because no workload changes session state. The other pools don't reset:
  fastpg's shared statements can't change session state, and `Pool.transaction()` resets
  within the COMMIT's round trip; psycopg's pool only rolls back a connection left in a
  transaction; psqlpy recycles connections without a query.
* **Autocommit.** Reads and single-row writes run in autocommit on every driver (fastpg's
  shared mode, asyncpg's `pool.fetch*`, psycopg with `autocommit=True`, psqlpy by default).
  Left at its default, psycopg would wrap every pooled query in BEGIN/COMMIT: two extra
  round trips. Transactions are explicit everywhere.
* **Prepared statements.** All four run the statements server-side prepared. psycopg
  prepares a statement on a connection after 5 executions; the warm-up runs every statement
  far more often than that on every connection.
* **Result format.** asyncpg, fastpg and psqlpy only use the binary format. psycopg can do
  either; its default text format measured equal or faster than `binary=True` in this suite
  (e.g. 5,939 vs 5,589 ops/s on `wide-50-c16`, 682 vs 578 on `fetch-1000-c4-typed`), so
  that's what it uses (`PsycopgAdapter(binary=True)` to try the other).
* **jsonb.** asyncpg returns jsonb as `str` and psycopg parses it with `json.loads`; both get
  `msgspec.json.decode` instead (`set_type_codec`, `set_json_loads`), so every driver parses
  JSON, with the fastest decoder each allows. psqlpy and fastpg decode jsonb natively.
* **Bulk writes.** `executemany` is all-or-nothing everywhere: asyncpg's and fastpg's are
  atomic by design; psycopg's (pipelined) and psqlpy's run in an explicit transaction, as
  in autocommit they would commit row by row. COPY is binary `COPY ... FROM STDIN` for all
  four (`copy_records_to_table`, `cursor.copy` + `set_types` + `write_row`, `copy_in`).
  Every driver gets the same pre-built list of tuples.
* **Not tuned:** anything the drivers' documentation doesn't recommend for a service, e.g.
  cursor or protocol internals.

## Output types and correctness

The read workloads run twice, in two modes:

* **Native rows**: each driver's default row type, with its default type mapping: fastpg
  `tuple`, asyncpg `Record`, psycopg `tuple`, psqlpy `dict`. This is the least work each
  driver can do. Where the defaults differ, it favours the other drivers: psqlpy returns
  `uuid` columns as `str` (no `UUID` object is built).
* **Typed rows** (`-typed`): every driver returns the same msgspec Structs, built the fastest
  documented way for each: fastpg `into=Person` (built in Rust), asyncpg `Person(*record)`,
  psycopg `row_factory=args_row(Person)`, psqlpy `result(as_tuple=True,
  custom_decoders={uuid column: UUID})` then `Person(*row)`. This is the cost an
  application that wants typed objects actually pays.

Before a read workload is timed, each driver's results for a set of arguments (first, middle
and last rows) are compared with rows computed in Python from the same formulas as the seed
data: typed rows must be the same Struct class with each field equal and of the expected type
(a subclass is accepted: asyncpg's `UUID` subclasses `uuid.UUID`); native rows must have
equal values column by column, psqlpy's `uuid` strings compared as UUIDs. After a write
workload, the server-side state is checked through a separate psycopg connection: the row
count equals the completed operations (warm-up included), every written value matches its
formula, account balances grew by exactly the number of updates, and transfers left the total
balance unchanged. A mismatch or a crash fails that measurement, is listed at the top of the
summary, and makes the run exit non-zero (the CI job fails). Numbers never do.

## Workloads

| workload | what one op is | tasks | why |
|---|---|---:|---|
| `point-c1` | one `people` row by primary key | 1 | Per-query latency with nothing else going on. |
| `point-c16` | same | 16 | The most common API query, at the pool's size. |
| `point-c64` | same | 64 | More tasks than connections: tasks queue for the pool (and fastpg may pipeline). |
| `point-c16-typed` | same, into a Struct | 16 | Decode included. |
| `page-20-c16`(`-typed`) | 20 consecutive `people` rows | 16 | A list endpoint. |
| `fetch-1000-c4`(`-typed`) | 1,000 `people` rows | 4 | A larger fetch (export, report): decode throughput. |
| `wide-50-c16`(`-typed`) | 50 `wide` rows: uuid, timestamptz, date, numeric, float8, bool, text, nullable text, jsonb, bytea, int8 | 16 | The expensive types, which `people` barely has. |
| `insert-c16` | INSERT one `events` row | 16 | The common write. |
| `update-c16` | UPDATE one `accounts` row by primary key | 16 | The other common write (random rows of 10,000: occasional lock waits). |
| `transaction-c16` | BEGIN; `SELECT ... FOR UPDATE` two accounts; two UPDATEs; one INSERT; COMMIT | 16 | A short business transaction: checkout plus six round trips. |
| `executemany-1000` | a 1,000-row INSERT batch | 1 | Bulk insert through executemany. |
| `copy-5000` | 5,000 rows by binary COPY | 1 | The fastest way to load rows. |
| `open-loop-1000`, `open-loop-4000` | point lookups arriving at that rate (Poisson) | – | Latency at equal load (below). |

`people` (100,000 rows, 10 columns) uses the same formulas as `docker/initdb/02-seed.sql`;
`wide` has 10,000 rows. Single-row writes go to logged tables; the bulk loads, which write
hundreds of thousands of rows per run, go to an unlogged table so the runner's disk isn't
part of the measurement. Bulk workloads also stop after 200 measured operations.

Arrays aren't in any workload because fastpg doesn't decode them yet (see
[Limitations](limitations.md)): that's a missing feature, not a result.

## Statistics

* **Runs.** One run is one (trial, workload, configuration). Before it, the write tables are
  emptied and the server runs a `CHECKPOINT`, so no run pays for evicting the dirty pages of
  the one before (the bulk loads leave up to a million rows; without this, later runs' tails
  grew by tens of milliseconds locally). `CHECKPOINT` needs superuser or `pg_checkpoint`;
  without either the run goes on and the environment records `checkpoint_between_runs=no`.
  Then: open a fresh pool, check the results, warm up for `WARMUP` seconds at the workload's
  own concurrency (at least 3 ops per task), collect garbage, measure for `DURATION`
  seconds, check the writes, close the pool.
* **Interleaving.** Each trial runs every workload; within a workload the configurations run
  back to back in an order shuffled per trial (seeded, recorded in the JSON as `position`).
  Drift over the run (a noisy neighbour, thermal throttling, table growth) is spread over
  every driver instead of always hitting whichever runs last.
* **Throughput** is the measured ops over the measured phase's wall time. The summary gives
  the median over trials with its min-max and interquartile range, the ratio of medians
  against `asyncpg`, and fastpg against the best non-fastpg configuration of each workload.
* **Latency** is per op (`perf_counter_ns` around the await): p50, p90, p99 and p99.9 for each
  trial, reported as the median over trials.
* **CPU per op** is the benchmark process's CPU time (every thread, so psqlpy's Rust runtime
  threads count) over the measured phase, per op. The server's CPU isn't included.
* **Open loop.** Requests arrive at a fixed rate from a seeded Poisson process, each as its
  own task on the pool, whether or not earlier ones have finished. Latency counts from the
  *scheduled* arrival, so a driver that keeps the event loop busy and delays later arrivals
  pays for it (no coordinated omission); *service p99* (dispatch to result) and *loop busy*
  (the event-loop thread's CPU share, i.e. the headroom left for the application) are
  reported alongside. A rate is marked overloaded, and stopped, once more than 2,000
  requests are outstanding.
* **Environment.** Each run records the git SHA (and whether the tree was dirty), CPU model
  and core counts, memory, OS, Python, event loop, PostgreSQL version and settings, the exact
  version of every driver, and the CI runner image.

## Limitations

* **Hosted runners are shared, noisy VMs** (4 vCPUs on `ubuntu-24.04`), with PostgreSQL on the
  same machine, competing with the client for CPU. Absolute numbers are indicative only and
  move between runs; compare drivers *within* a run. The interleaving and medians reduce,
  but don't remove, the noise: treat differences of a few percent as ties.
* **No network.** Client and server talk over loopback, so round trips are short and client
  overhead looms larger than over a real network, where the RTT dominates every driver's
  latency alike. Sequential (`point-c1`) gaps shrink accordingly; concurrent throughput and
  CPU per op are what carry over.
* **Client-side cost only.** The server's work is identical across drivers, but its CPU isn't
  measured, and on a 4-vCPU runner it limits the concurrent workloads for everyone.
* **One process, one event loop (uvloop), one pool size (16), small hot tables** that fit in
  shared buffers. Other shapes (thousands of connections, cold data, big rows, pgbouncer)
  aren't covered. CI's short durations (3 s per run) favour repeatability over steady state.
* **The configurations aren't identical by nature**: fastpg's pool can pipeline and the
  others can't (hence `fastpg-inflight1`); asyncpg resets connections on release (hence
  `asyncpg-noreset`); psqlpy's pool opens its connections lazily (the warm-up opens them).
* **We wrote this suite.** It covers query shapes an API service runs, and the results include
  the workloads where fastpg ties or loses. Rerun it on your hardware and with your queries;
  issues and pull requests that make another driver faster here are welcome.
