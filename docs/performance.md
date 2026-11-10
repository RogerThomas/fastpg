# Benchmarks

fastpg is compared with [asyncpg](https://github.com/MagicStack/asyncpg),
[psycopg 3](https://www.psycopg.org/psycopg3/) and
[psqlpy](https://github.com/psqlpy-python/psqlpy).
These numbers are from
[run 42791890769](https://github.com/RogerThomas/fastpg/actions/runs/42791890769) of the
[Benchmarks workflow](https://github.com/RogerThomas/fastpg/actions/workflows/benchmarks.yml)
on `main`, on 2026-11-10.
It tested fastpg 0.2.1, installed from PyPI.

## How to read them

* They come from one shared x86_64 GitHub runner, with PostgreSQL on the same machine.
  GitHub's arm64 runner gives similar ratios
  ([run 42794789116](https://github.com/RogerThomas/fastpg/actions/runs/42794789116)).
* Every driver uses its own pool, with its recommended settings, and returns the same msgspec
  Structs.
* asyncpg runs with its pool's session reset turned off, because fastpg's pool doesn't reset
  sessions either.
* The smallest lead is `execute_many` of 1,000 rows: 1.44x.
* Over a real network, the gaps on single queries get smaller.

## Throughput

Higher is better.
"Fastest other driver" is the fastest of the others on each workload.

| workload | fastpg | fastest other driver | speed-up |
|---|---:|---:|---:|
| Primary-key lookup, 1 task | 5,825/s | 4,157/s (asyncpg) | 1.40x |
| Primary-key lookup, 16 tasks | 24,867/s | 10,341/s (asyncpg) | 2.40x |
| Primary-key lookup, 64 tasks | 34,124/s | 10,485/s (asyncpg) | 3.25x |
| Primary-key lookup into a Struct, 16 tasks | 23,889/s | 10,231/s (asyncpg) | 2.33x |
| Page of 20 rows, 16 tasks | 12,653/s | 6,242/s (asyncpg) | 2.03x |
| Page of 20 rows into Structs, 16 tasks | 12,181/s | 5,981/s (asyncpg) | 2.04x |
| 1,000 rows, 4 tasks | 890k rows/s | 409k rows/s (asyncpg) | 2.17x |
| 1,000 rows into Structs, 4 tasks | 813k rows/s | 358k rows/s (asyncpg) | 2.27x |
| 50 wide rows (13 columns), 16 tasks | 5,683/s | 3,402/s (asyncpg) | 1.67x |
| 50 wide rows into Structs, 16 tasks | 4,955/s | 3,248/s (asyncpg) | 1.53x |
| Single-row INSERT, 16 tasks | 23,817/s | 9,584/s (asyncpg) | 2.48x |
| Single-row UPDATE, 16 tasks | 22,924/s | 10,491/s (asyncpg) | 2.19x |
| Transaction of 4 statements, 16 tasks | 4,004/s | 2,763/s (asyncpg) | 1.45x |
| `execute_many`, 1,000 rows | 180k rows/s | 125k rows/s (asyncpg) | 1.44x |
| Binary COPY, 5,000 rows | 932k rows/s | 632k rows/s (psycopg) | 1.48x |

The pool has 16 connections.
With 64 tasks, fastpg sends several queries to each connection.
With that turned off (`max_inflight_per_conn=1`), it does 22,182/s, still 2.12x.

## Latency

Primary-key lookups arriving at a steady 1,000 or 4,000 per second.
Lower is better.

| | fastpg | asyncpg | psycopg | psqlpy |
|---|---:|---:|---:|---:|
| p99 at 1,000/s | 1,952 µs | 2,109 µs | 2,082 µs | 2,410 µs |
| p99.9 at 1,000/s | 2,176 µs | 2,446 µs | 2,412 µs | 3,335 µs |
| p99 at 4,000/s | 1,471 µs | 2,265 µs | 2,615 µs | 8,374 µs |
| p99.9 at 4,000/s | 2,627 µs | 3,706 µs | 3,879 µs | 10,853 µs |

At 4,000/s, fastpg's p99.9 is about 30% lower than asyncpg's and psycopg's.

## Run them yourself

```sh
task db-up bench-ci
```

How the suite works, and the full results: [Methodology](benchmarks.md).
