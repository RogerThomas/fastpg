# Getting started

## Install

```sh
uv add fastpg                  # or: pip install fastpg
uv add 'fastpg[pydantic]'      # with pydantic v2 support
```

* Python 3.14.3 or later, on macOS or Linux. Windows isn't supported.
* fastpg isn't on PyPI yet. Until its first release, build it from source, which needs a
  Rust toolchain and takes a few minutes:
  `uv add git+https://github.com/RogerThomas/fastpg`.
* To work on fastpg itself, run `uv sync --all-groups` (or `task build`) in a checkout.

## The example database

The examples in these docs run against the repository's PostgreSQL, started with Docker and
[Task](https://taskfile.dev):

```sh
task db-up      # PostgreSQL 18.6 on 127.0.0.1:55432; user, password and database: fastpg
task example    # runs ./examples/basic.py
task db-down    # stops it and discards the data
```

Each start seeds a `people` table with 100,000 rows: `id` (1 to 100,000), `name`
(`person-<id>`), `age` (`18 + id % 60`), `email` (NULL every 7th row), `score`, `balance`
(`numeric`), `active`, `external_id` (`uuid`), `created_at` (`timestamptz`) and `birthday`
(NULL every 11th row).

## A first query

```python
import asyncio

import msgspec

import fastpg


class Person(msgspec.Struct):
    name: str
    age: int
    email: str | None


async def main() -> None:
    dsn = "postgresql://fastpg:fastpg@127.0.0.1:55432/fastpg"
    async with await fastpg.connect(dsn) as conn:
        query = "SELECT name, age, email FROM people WHERE id = $1"
        person = await conn.select_one(query, 42, into=Person)
        print(person)  # Person(name='person-42', age=60, email=None)


asyncio.run(main())
```

## Conventions

* `fastpg.connect()` opens one connection, bound to the running event loop. For concurrent
  requests, use a [pool](pool.md): it has the same query methods.
* Leaving `async with` (or `await conn.close()`) closes gracefully: queries in flight
  complete, then the session ends, without waiting for the server to acknowledge it.
  `conn.terminate()` closes at once and fails queries in flight with
  `ConnectionClosedError`, and so does a `close()` that is cancelled (e.g. by
  `asyncio.timeout`).
* The other pages show the body of `main()`: `conn` is an open connection and `dsn` is the
  DSN above. Examples that write create a temporary table.
