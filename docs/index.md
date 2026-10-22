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
* **Fast bulk writes.** `copy_in`, `insert_many`, `execute_many`. See
  [Bulk writes](guide/writes.md).
* **Transactions, TLS and clear errors.** See [Transactions](guide/transactions.md),
  [TLS](guide/tls.md) and [Errors](guide/errors.md).
* **pydantic too.** If your app uses pydantic, see [pydantic models](guide/pydantic.md).

## Speed

| workload | fastpg | fastest other driver | speed-up |
|---|---:|---:|---:|
| Primary-key lookup into a Struct, 16 tasks | 39,774/s | 17,144/s (asyncpg) | 2.32x |
| Page of 20 rows into Structs, 16 tasks | 19,002/s | 9,094/s (asyncpg) | 2.09x |
| 1,000 rows into Structs, 4 tasks | 1.03M rows/s | 476k rows/s (asyncpg) | 2.15x |
| Single-row INSERT, 16 tasks | 35,860/s | 16,087/s (asyncpg) | 2.23x |
| Binary COPY, 5,000 rows | 1.16M rows/s | 823k rows/s (psycopg) | 1.41x |

All workloads and how they're measured: [Benchmarks](performance.md).

fastpg is alpha software. See what it can't do yet in [Limitations](limitations.md).
