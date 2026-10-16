# Getting started

fastpg builds from a checkout and runs against the repository's bundled PostgreSQL.

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

## Requirements and install

* Python 3.14.3 or later, on macOS or Linux.
* A Rust toolchain (1.88 or later) and [uv](https://docs.astral.sh/uv/); the first build
  takes a minute or two.
* Docker for the example database, and optionally [Task](https://taskfile.dev).

```sh
uv add /path/to/fastpg                     # from another project, msgspec only
uv add /path/to/fastpg --extra pydantic    # with pydantic models
uv sync --all-groups                       # or, to work on fastpg itself (task build)
```

## The example database

```sh
task db-up      # PostgreSQL 18.6 on 127.0.0.1:55432; user, password and database: fastpg
task example    # runs ./examples/basic.py
task db-down    # stops it and discards the data
```

Each start seeds a `people` table with 100,000 rows: `id` (1 to 100,000), `name`
(`person-<id>`), `age` (`18 + id % 60`), `email` (NULL every 7th row), `score`, `balance`
(`numeric`), `active`, `external_id` (`uuid`), `created_at` (`timestamptz`) and `birthday`
(NULL every 11th row).

## Conventions

* `fastpg.connect()` opens one connection, bound to the running event loop. For concurrent
  requests, use a [pool](pool.md).
* Leaving `async with` (or `await conn.close()`) closes gracefully: queries in flight
  complete, then the session ends, without waiting for the server to acknowledge it.
  `conn.terminate()` closes at once and fails queries in flight with
  `ConnectionClosedError`, and so does a `close()` that is cancelled (e.g. by
  `asyncio.timeout`).
* The other pages show the body of `main()`: `conn` is an open connection and `dsn` is the
  DSN above. Examples that write create a temporary table.
