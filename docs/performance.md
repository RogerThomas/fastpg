# Benchmarks

fastpg is compared with [asyncpg](https://github.com/MagicStack/asyncpg),
[psycopg 3](https://www.psycopg.org/psycopg3/) and
[psqlpy](https://github.com/psqlpy-python/psqlpy).
These numbers are from
[run 39671855945](https://github.com/RogerThomas/fastpg/actions/runs/39671855945) of the
[Benchmarks workflow](https://github.com/RogerThomas/fastpg/actions/workflows/benchmarks.yml)
on `main`, on 2026-10-21.

## How to read them

* They come from one shared GitHub runner, with PostgreSQL on the same machine.
* Every driver uses its own pool, with its recommended settings, and returns the same msgspec
  Structs.
* asyncpg runs with its pool's session reset turned off, because fastpg's pool doesn't reset
  sessions either.
* The smallest lead is a transaction: 1.13x.
* Over a real network, the gaps on single queries get smaller.

## Throughput

Higher is better.
"Fastest other driver" is the fastest of the others on each workload.

| workload | fastpg | fastest other driver | speed-up |
|---|---:|---:|---:|
| Primary-key lookup, 1 task | 9,284/s | 6,310/s (asyncpg) | 1.47x |
| Primary-key lookup, 16 tasks | 38,402/s | 17,262/s (asyncpg) | 2.22x |
| Primary-key lookup, 64 tasks | 54,019/s | 17,577/s (asyncpg) | 3.07x |
| Primary-key lookup into a Struct, 16 tasks | 39,774/s | 17,144/s (asyncpg) | 2.32x |
| Page of 20 rows, 16 tasks | 19,270/s | 9,480/s (asyncpg) | 2.03x |
| Page of 20 rows into Structs, 16 tasks | 19,002/s | 9,094/s (asyncpg) | 2.09x |
| 1,000 rows, 4 tasks | 1.14M rows/s | 545k rows/s (asyncpg) | 2.08x |
| 1,000 rows into Structs, 4 tasks | 1.03M rows/s | 476k rows/s (asyncpg) | 2.15x |
| 50 wide rows (13 columns), 16 tasks | 7,806/s | 4,889/s (asyncpg) | 1.60x |
| 50 wide rows into Structs, 16 tasks | 6,522/s | 4,668/s (asyncpg) | 1.40x |
| Single-row INSERT, 16 tasks | 35,860/s | 16,087/s (asyncpg) | 2.23x |
| Single-row UPDATE, 16 tasks | 35,766/s | 17,718/s (asyncpg) | 2.02x |
| Transaction of 4 statements, 16 tasks | 5,893/s | 5,205/s (asyncpg) | 1.13x |
| `execute_many`, 1,000 rows | 256k rows/s | 177k rows/s (asyncpg) | 1.44x |
| Binary COPY, 5,000 rows | 1.16M rows/s | 823k rows/s (psycopg) | 1.41x |

The pool has 16 connections.
With 64 tasks, fastpg sends several queries to each connection.
With that turned off (`max_inflight_per_conn=1`), it does 37,599/s, still 2.14x.

## Latency

Primary-key lookups arriving at a steady 1,000 or 4,000 per second.
Lower is better.

| | fastpg | asyncpg | psycopg | psqlpy |
|---|---:|---:|---:|---:|
| p99 at 1,000/s | 855 µs | 961 µs | 1,071 µs | 1,458 µs |
| p99.9 at 1,000/s | 1,092 µs | 1,364 µs | 1,566 µs | 2,473 µs |
| p99 at 4,000/s | 1,034 µs | 1,270 µs | 1,368 µs | 2,980 µs |
| p99.9 at 4,000/s | 2,917 µs | 3,114 µs | 2,946 µs | 5,386 µs |

At 4,000/s, fastpg's p99.9 is within 7% of asyncpg's and psycopg's: call it a tie.

## Run them yourself

```sh
task db-up bench-ci
```

How the suite works, and the full results: [Methodology](benchmarks.md).
