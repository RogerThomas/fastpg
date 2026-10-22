<h1>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/RogerThomas/fastpg/main/docs/assets/wordmark-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/RogerThomas/fastpg/main/docs/assets/wordmark-light.svg">
    <img alt="fastpg" src="https://raw.githubusercontent.com/RogerThomas/fastpg/main/docs/assets/wordmark-light.svg" height="56">
  </picture>
</h1>

<a href="https://app.codspeed.io/RogerThomas/fastpg?utm_source=badge"><img src="https://img.shields.io/endpoint?url=https://codspeed.io/badge.json" alt="CodSpeed"/></a>

fastpg is a fast async PostgreSQL client for Python, written in Rust.
It turns rows straight into [msgspec](https://msgspec.dev/) Structs, so you get typed objects
back with no extra code.
On common queries it is about twice as fast as asyncpg, psycopg 3 and psqlpy.

## Install

```sh
uv add fastpg
```

## Quickstart

```python
import asyncio

import msgspec

import fastpg


class Person(msgspec.Struct):
    id: int
    name: str
    email: str | None


async def main() -> None:
    dsn = "postgresql://fastpg:fastpg@127.0.0.1:55432/fastpg"
    async with await fastpg.create_pool(dsn) as pool:
        query = "SELECT id, name, email FROM people WHERE id = $1"
        person = await pool.select_one(query, 42, into=Person)
        print(person)  # Person(id=42, name='person-42', email=None)

        query = "SELECT id, name, email FROM people WHERE id <= $1 ORDER BY id"
        people = await pool.select_many(query, 3, into=Person)
        print(len(people))  # 3


asyncio.run(main())
```

## Arrays

Array columns come back as lists, and lists go in as arrays:

```python
class AgeGroup(msgspec.Struct):
    age: int
    names: list[str]


query = """
SELECT age, array_agg(name ORDER BY id) AS names
FROM people WHERE id = ANY($1) GROUP BY age ORDER BY age
"""
print(await pool.select_many(query, [42, 43, 102], into=AgeGroup))
# [AgeGroup(age=60, names=['person-42', 'person-102']), AgeGroup(age=61, names=['person-43'])]
```

## Features

* **Typed rows.** Pass `into=Person` and get Structs back. Dicts, NamedTuples and
  [pydantic](https://rogerthomas.github.io/fastpg/guide/pydantic/) models work too.
* **A pool you query directly.** `pool.select_one(...)` needs no `acquire()`.
* **Safe parameters.** Use `$1`, `:name` or Python t-strings. Values are always sent apart
  from the SQL.
* **Fast bulk writes.** `copy_in`, `insert_many` and `execute_many`.
* **The rest you need.** Transactions, TLS, and clear errors such as `UniqueViolationError`.
* **Fully typed.** Checks cleanly with `pyright --strict`.

## Benchmarks

| workload | fastpg | fastest other driver | speed-up |
|---|---:|---:|---:|
| Primary-key lookup into a Struct, 16 tasks | 39,774/s | 17,144/s (asyncpg) | **2.32x** |
| Page of 20 rows into Structs, 16 tasks | 19,002/s | 9,094/s (asyncpg) | **2.09x** |
| 1,000 rows into Structs, 4 tasks | 1.03M rows/s | 476k rows/s (asyncpg) | **2.15x** |
| Single-row INSERT, 16 tasks | 35,860/s | 16,087/s (asyncpg) | **2.23x** |
| Binary COPY, 5,000 rows | 1.16M rows/s | 823k rows/s (psycopg) | **1.41x** |

Measured in CI on one shared GitHub runner; the smallest lead, a transaction, is 1.13x
([details](https://rogerthomas.github.io/fastpg/performance/)).

## Documentation

**[rogerthomas.github.io/fastpg](https://rogerthomas.github.io/fastpg/)**

fastpg is alpha software. MIT licensed.
