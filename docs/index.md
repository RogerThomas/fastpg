<h1 id="fastpg" markdown>
![fastpg](assets/wordmark-light.svg#only-light){ width="240" }
![fastpg](assets/wordmark-dark.svg#only-dark){ width="240" }
</h1>

fastpg is a fast async PostgreSQL client for Python, written in Rust.
It turns rows straight into [msgspec](https://msgspec.dev/) Structs, so you get typed objects
back with no extra code.

```sh
uv add fastpg
```

```python
import asyncio

import msgspec

import fastpg


class Person(msgspec.Struct):
    name: str
    age: int


async def main() -> None:
    dsn = "postgresql://fastpg:fastpg@127.0.0.1:55432/fastpg"
    async with await fastpg.create_pool(dsn) as pool:
        query = "SELECT name, age FROM people WHERE id = $1"
        person = await pool.select_one(query, 42, into=Person)
        print(person)  # Person(name='person-42', age=60)


asyncio.run(main())
```

New here? Start with [Getting started](guide/getting-started.md).

## What you get

* **Typed rows.** `into=Person` gives you Structs. See [Structs](guide/structs.md).
* **A pool you query directly.** No `acquire()` for a single query. See
  [Connection pool](guide/pool.md).
* **Safe parameters.** `$1`, `:name` or t-strings. See [Queries](guide/queries.md).
* **Arrays and grouping.** `list[X]` fields, lists as parameters, and `select_and_group`
  for one-to-many rows. See [Arrays and grouping](guide/lists.md).
* **Fast bulk writes.** `copy_in`, `insert_many`, `execute_many`. See
  [Bulk writes](guide/writes.md).
* **Transactions, TLS and clear errors.** See [Transactions](guide/transactions.md),
  [TLS](guide/tls.md) and [Errors](guide/errors.md).
* **pydantic too.** If your app uses pydantic, see [pydantic models](guide/pydantic.md).

## Speed

<!-- home-speed:start -->

**On primary-key lookups, pages of rows, 1,000-row fetches and single-row writes, fastpg does 2.2–3.0x the work of the fastest other driver.**

<div class="bench-chart" role="img" aria-label="Bar chart of throughput relative to the fastest other driver: fastpg does 2.81x to 2.97x on each of the two workloads.">
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
</div>
<!-- home-speed:end -->

For a more detailed breakdown, and how the benchmarks were run, see [Benchmarks](performance.md).

fastpg is alpha software. See what it can't do yet in [Limitations](limitations.md).

fastpg follows Semantic Versioning. Names starting with `_` are internal and can change in any release.
