# Benchmarks

fastpg is compared with [asyncpg](https://github.com/MagicStack/asyncpg),
[psycopg 3](https://www.psycopg.org/psycopg3/) and
[psqlpy](https://github.com/psqlpy-python/psqlpy).

<!-- bench-chart:start -->

**On primary-key lookups, pages of rows, 1,000-row fetches and single-row writes, fastpg does 2.2–3.0x the work of the fastest other driver.**

Its smallest lead is binary COPY of 5,000 rows: 1.23x.

<div class="bench-chart" role="img" aria-label="Bar chart of throughput relative to the fastest other driver: fastpg does 2.18x to 2.97x on each of the six workloads in the table below.">
  <div class="bench-axis">
    Throughput, relative to the fastest other driver (1x).<br>
    A task is one asyncio task sending queries one after another. The tasks run at the same time, sharing one pool of 16 connections per driver.
  </div>
  <div class="bench-group">
    <div class="bench-caption">Primary-key lookup, 64 tasks</div>
    <div class="bench-row bench-row--fastpg" title="fastpg: 188,227 queries/s (2.97x of asyncpg)"><span class="bench-name">fastpg</span><span class="bench-track"><span class="bench-bar" style="width: 99.06%"><span class="bench-value">2.97x</span></span></span></div>
    <div class="bench-row bench-row--asyncpg" title="asyncpg: 63,340 queries/s (0.34x of fastpg)"><span class="bench-name">asyncpg</span><span class="bench-track"><span class="bench-bar" style="width: 33.33%"><span class="bench-value">1.00x</span></span></span></div>
    <div class="bench-row bench-row--psycopg" title="psycopg: 31,507 queries/s (0.17x of fastpg)"><span class="bench-name">psycopg</span><span class="bench-track"><span class="bench-bar" style="width: 16.58%"><span class="bench-value">0.50x</span></span></span></div>
    <div class="bench-row bench-row--psqlpy" title="psqlpy: 24,116 queries/s (0.13x of fastpg)"><span class="bench-name">psqlpy</span><span class="bench-track"><span class="bench-bar" style="width: 12.69%"><span class="bench-value">0.38x</span></span></span></div>
  </div>
  <div class="bench-group">
    <div class="bench-caption">1,000 rows into Structs, 4 tasks</div>
    <div class="bench-row bench-row--fastpg" title="fastpg: 2.31M rows/s (2.81x of psycopg)"><span class="bench-name">fastpg</span><span class="bench-track"><span class="bench-bar" style="width: 93.72%"><span class="bench-value">2.81x</span></span></span></div>
    <div class="bench-row bench-row--psycopg" title="psycopg: 823k rows/s (0.36x of fastpg)"><span class="bench-name">psycopg</span><span class="bench-track"><span class="bench-bar" style="width: 33.33%"><span class="bench-value">1.00x</span></span></span></div>
    <div class="bench-row bench-row--asyncpg" title="asyncpg: 761k rows/s (0.33x of fastpg)"><span class="bench-name">asyncpg</span><span class="bench-track"><span class="bench-bar" style="width: 30.82%"><span class="bench-value">0.92x</span></span></span></div>
    <div class="bench-row bench-row--psqlpy" title="psqlpy: 518k rows/s (0.22x of fastpg)"><span class="bench-name">psqlpy</span><span class="bench-track"><span class="bench-bar" style="width: 20.97%"><span class="bench-value">0.63x</span></span></span></div>
  </div>
  <div class="bench-group">
    <div class="bench-caption">Page of 20 rows into Structs, 16 tasks</div>
    <div class="bench-row bench-row--fastpg" title="fastpg: 66,242 queries/s (2.46x of asyncpg)"><span class="bench-name">fastpg</span><span class="bench-track"><span class="bench-bar" style="width: 81.98%"><span class="bench-value">2.46x</span></span></span></div>
    <div class="bench-row bench-row--asyncpg" title="asyncpg: 26,936 queries/s (0.41x of fastpg)"><span class="bench-name">asyncpg</span><span class="bench-track"><span class="bench-bar" style="width: 33.33%"><span class="bench-value">1.00x</span></span></span></div>
    <div class="bench-row bench-row--psycopg" title="psycopg: 22,316 queries/s (0.34x of fastpg)"><span class="bench-name">psycopg</span><span class="bench-track"><span class="bench-bar" style="width: 27.62%"><span class="bench-value">0.83x</span></span></span></div>
    <div class="bench-row bench-row--psqlpy" title="psqlpy: 12,644 queries/s (0.19x of fastpg)"><span class="bench-name">psqlpy</span><span class="bench-track"><span class="bench-bar" style="width: 15.65%"><span class="bench-value">0.47x</span></span></span></div>
  </div>
  <div class="bench-group">
    <div class="bench-caption">Single-row INSERT, 16 tasks</div>
    <div class="bench-row bench-row--fastpg" title="fastpg: 153,807 queries/s (2.26x of asyncpg)"><span class="bench-name">fastpg</span><span class="bench-track"><span class="bench-bar" style="width: 75.41%"><span class="bench-value">2.26x</span></span></span></div>
    <div class="bench-row bench-row--asyncpg" title="asyncpg: 67,990 queries/s (0.44x of fastpg)"><span class="bench-name">asyncpg</span><span class="bench-track"><span class="bench-bar" style="width: 33.33%"><span class="bench-value">1.00x</span></span></span></div>
    <div class="bench-row bench-row--psycopg" title="psycopg: 49,224 queries/s (0.32x of fastpg)"><span class="bench-name">psycopg</span><span class="bench-track"><span class="bench-bar" style="width: 24.13%"><span class="bench-value">0.72x</span></span></span></div>
    <div class="bench-row bench-row--psqlpy" title="psqlpy: 24,988 queries/s (0.16x of fastpg)"><span class="bench-name">psqlpy</span><span class="bench-track"><span class="bench-bar" style="width: 12.25%"><span class="bench-value">0.37x</span></span></span></div>
  </div>
  <div class="bench-group">
    <div class="bench-caption">Single-row UPDATE, 16 tasks</div>
    <div class="bench-row bench-row--fastpg" title="fastpg: 167,282 queries/s (2.21x of asyncpg)"><span class="bench-name">fastpg</span><span class="bench-track"><span class="bench-bar" style="width: 73.79%"><span class="bench-value">2.21x</span></span></span></div>
    <div class="bench-row bench-row--asyncpg" title="asyncpg: 75,569 queries/s (0.45x of fastpg)"><span class="bench-name">asyncpg</span><span class="bench-track"><span class="bench-bar" style="width: 33.33%"><span class="bench-value">1.00x</span></span></span></div>
    <div class="bench-row bench-row--psycopg" title="psycopg: 54,074 queries/s (0.32x of fastpg)"><span class="bench-name">psycopg</span><span class="bench-track"><span class="bench-bar" style="width: 23.85%"><span class="bench-value">0.72x</span></span></span></div>
    <div class="bench-row bench-row--psqlpy" title="psqlpy: 25,376 queries/s (0.15x of fastpg)"><span class="bench-name">psqlpy</span><span class="bench-track"><span class="bench-bar" style="width: 11.19%"><span class="bench-value">0.34x</span></span></span></div>
  </div>
  <div class="bench-group">
    <div class="bench-caption">Primary-key lookup into a Struct, 16 tasks</div>
    <div class="bench-row bench-row--fastpg" title="fastpg: 149,250 queries/s (2.18x of asyncpg)"><span class="bench-name">fastpg</span><span class="bench-track"><span class="bench-bar" style="width: 72.50%"><span class="bench-value">2.18x</span></span></span></div>
    <div class="bench-row bench-row--asyncpg" title="asyncpg: 68,617 queries/s (0.46x of fastpg)"><span class="bench-name">asyncpg</span><span class="bench-track"><span class="bench-bar" style="width: 33.33%"><span class="bench-value">1.00x</span></span></span></div>
    <div class="bench-row bench-row--psycopg" title="psycopg: 41,043 queries/s (0.27x of fastpg)"><span class="bench-name">psycopg</span><span class="bench-track"><span class="bench-bar" style="width: 19.94%"><span class="bench-value">0.60x</span></span></span></div>
    <div class="bench-row bench-row--psqlpy" title="psqlpy: 23,568 queries/s (0.16x of fastpg)"><span class="bench-name">psqlpy</span><span class="bench-track"><span class="bench-bar" style="width: 11.45%"><span class="bench-value">0.34x</span></span></span></div>
  </div>
</div>

## The raw numbers

| workload | speed-up | fastpg | fastest other driver |
|---|---:|---:|---:|
| Primary-key lookup, 64 tasks | **2.97x** | 188,227/s | 63,340/s (asyncpg) |
| 1,000 rows into Structs, 4 tasks | **2.81x** | 2.31M rows/s | 823k rows/s (psycopg) |
| Page of 20 rows into Structs, 16 tasks | **2.46x** | 66,242/s | 26,936/s (asyncpg) |
| Single-row INSERT, 16 tasks | **2.26x** | 153,807/s | 67,990/s (asyncpg) |
| Single-row UPDATE, 16 tasks | **2.21x** | 167,282/s | 75,569/s (asyncpg) |
| Primary-key lookup into a Struct, 16 tasks | **2.18x** | 149,250/s | 68,617/s (asyncpg) |

<!-- bench-chart:end -->

## How to read them

* They come from an x86_64 AWS m8a.2xlarge, 8 vCPU, with PostgreSQL on the same machine.
  An arm64 AWS m9g.2xlarge, also 8 vCPU, gives similar ratios.
* Every driver uses its own pool, with its recommended settings, and returns the same msgspec
  Structs.
* asyncpg runs with its pool's session reset turned off, because fastpg's pool only resets a
  session after a transaction.
* Over a real network, the gaps on single queries get smaller.

The full results for all 15 workloads, and the latency numbers:
[Methodology](benchmarks.md#full-results).

## Run them yourself

These numbers come from:

```sh
task bench-aws -- --fastpg-version 0.2.1
```

On your own machine:

```sh
task db-up bench-ci
```
