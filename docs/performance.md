# Benchmarks

fastpg is compared with [asyncpg](https://github.com/MagicStack/asyncpg),
[psycopg 3](https://www.psycopg.org/psycopg3/) and
[psqlpy](https://github.com/psqlpy-python/psqlpy).
These numbers are from 2026-11-10.
They tested fastpg 0.2.1, installed from PyPI.

## How to read them

* They come from an x86_64 AWS m8a.2xlarge, 8 vCPU, with PostgreSQL on the same machine.
  An arm64 AWS m9g.2xlarge, also 8 vCPU, gives similar ratios.
* Every driver uses its own pool, with its recommended settings, and returns the same msgspec
  Structs.
* asyncpg runs with its pool's session reset turned off, because fastpg's pool only resets a
  session after a transaction.
* The smallest lead is binary COPY of 5,000 rows: 1.23x.
* Over a real network, the gaps on single queries get smaller.

## Throughput

Higher is better.
"Fastest other driver" is the fastest of the others on each workload.

| workload | fastpg | fastest other driver | speed-up |
|---|---:|---:|---:|
| Primary-key lookup, 1 task | 49,994/s | 35,562/s (asyncpg) | 1.41x |
| Primary-key lookup, 16 tasks | 154,049/s | 69,483/s (asyncpg) | 2.22x |
| Primary-key lookup, 64 tasks | 188,227/s | 63,340/s (asyncpg) | 2.97x |
| Primary-key lookup into a Struct, 16 tasks | 149,250/s | 68,617/s (asyncpg) | 2.18x |
| Page of 20 rows, 16 tasks | 72,070/s | 28,748/s (asyncpg) | 2.51x |
| Page of 20 rows into Structs, 16 tasks | 66,242/s | 26,936/s (asyncpg) | 2.46x |
| 1,000 rows, 4 tasks | 2.57M rows/s | 884k rows/s (psycopg) | 2.91x |
| 1,000 rows into Structs, 4 tasks | 2.31M rows/s | 823k rows/s (psycopg) | 2.81x |
| 50 wide rows (13 columns), 16 tasks | 17,168/s | 10,600/s (asyncpg) | 1.62x |
| 50 wide rows into Structs, 16 tasks | 14,884/s | 10,044/s (asyncpg) | 1.48x |
| Single-row INSERT, 16 tasks | 153,807/s | 67,990/s (asyncpg) | 2.26x |
| Single-row UPDATE, 16 tasks | 167,282/s | 75,569/s (asyncpg) | 2.21x |
| Transaction of 4 statements, 16 tasks | 29,204/s | 20,489/s (asyncpg) | 1.43x |
| `execute_many`, 1,000 rows | 407k rows/s | 254k rows/s (asyncpg) | 1.60x |
| Binary COPY, 5,000 rows | 1.37M rows/s | 1.11M rows/s (psycopg) | 1.23x |

The pool has 16 connections.
With 64 tasks, fastpg sends several queries to each connection.
With that turned off (`max_inflight_per_conn=1`), it does 136,171/s, still 2.15x.

## Latency

Primary-key lookups arriving at a steady 1,000 or 4,000 per second.
Lower is better.

| | fastpg | asyncpg | psycopg | psqlpy |
|---|---:|---:|---:|---:|
| p99 at 1,000/s | 617 µs | 636 µs | 678 µs | 747 µs |
| p99.9 at 1,000/s | 652 µs | 672 µs | 732 µs | 807 µs |
| p99 at 4,000/s | 598 µs | 633 µs | 656 µs | 817 µs |
| p99.9 at 4,000/s | 624 µs | 690 µs | 713 µs | 961 µs |

At 4,000/s, fastpg's p99.9 is about 10% lower than asyncpg's and 12% lower than psycopg's.

## Run them yourself

```sh
task db-up bench-ci
```

How the suite works, and the full results: [Methodology](benchmarks.md).
