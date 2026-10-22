# Getting started

## Install

```sh
uv add fastpg          # or: pip install fastpg
```

fastpg needs Python 3.14 or newer, on Linux or macOS.
msgspec is installed with it.
For pydantic models, run `uv add 'fastpg[pydantic]'` instead.

## The example database

The examples in these docs use a small example database.
To run it, clone the [repository](https://github.com/RogerThomas/fastpg) and, with Docker and
[Task](https://taskfile.dev) installed, run:

```sh
task db-up      # PostgreSQL on 127.0.0.1:55432
task db-down    # stop it and delete the data
```

The user, password and database name are all `fastpg`.
It has a `people` table with 100,000 rows.
Its columns include `id`, `name` (`'person-<id>'`), `age` and `email` (NULL on every 7th row).

## Your first query

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

`fastpg.connect()` opens one connection.
`async with` closes it when the block ends.
`into=Person` turns the row into a `Person`.
Without `into=`, you get a tuple.

## One connection or a pool?

* **One connection** for a script or a job that runs one query at a time.
* **A [pool](pool.md)** for an app that serves many requests at once, such as a web app.

Both have the same query methods.

```python
async with await fastpg.create_pool(dsn) as pool:
    query = "SELECT name, age, email FROM people WHERE id = $1"
    person = await pool.select_one(query, 42, into=Person)
```

## Reading the other pages

The other pages show only the body of `main()`.
In them, `conn` is an open connection and `dsn` is the DSN above.

Next: [Queries](queries.md).
