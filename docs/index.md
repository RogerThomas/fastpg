<h1 id="fastpg" markdown>
![fastpg](assets/wordmark-light.svg#only-light){ width="240" }
![fastpg](assets/wordmark-dark.svg#only-dark){ width="240" }
</h1>

fastpg is an async PostgreSQL client for Python 3.14, written in Rust on
[xitca-postgres](https://github.com/HFQR/xitca-web/tree/main/postgres) and
[PyO3](https://pyo3.rs). It decodes binary rows straight into
[msgspec](https://jcristharif.com/msgspec/) Structs or pydantic models, and runs the whole
protocol on the asyncio event loop's thread, with no threads of its own.

```python
import asyncio

import fastpg
import msgspec


class Person(msgspec.Struct):
    name: str
    age: int


async def main() -> None:
    dsn = "postgresql://fastpg:fastpg@127.0.0.1:55432/fastpg"
    async with await fastpg.connect(dsn) as conn:
        query = "SELECT name, age FROM people WHERE id = $1"
        person = await conn.select_one(query, 42, into=Person)
        print(person)  # Person(name='person-42', age=60)


asyncio.run(main())
```

| one CPU, Linux | fastpg | asyncpg |
|---|---:|---:|
| Lookups by id, 16 tasks through the pool | 64.9k ops/s | 25.3k ops/s |
| 10,000-row fetch | 1.73M rows/s | 0.71M rows/s |
| CPU per request | 23.5 µs | 34 µs |

Setup, more drivers and caveats: [Performance](performance.md).

## Install

fastpg is an early prototype, not on PyPI yet (see [Limitations](limitations.md)). It needs
Python 3.14.3 or later on macOS or Linux, and builds from a checkout with a Rust toolchain:

```sh
uv add /path/to/fastpg                     # msgspec Structs
uv add /path/to/fastpg --extra pydantic    # and pydantic models
```

[Getting started](guide/getting-started.md) runs the examples against a local database.

## Features

* **Typed rows**: `into=Person` builds msgspec Structs or pydantic v2 models from the binary
  values, with no intermediate tuples or dicts. See [Structs](guide/structs.md).
* **Prepared, binary, pipelined**: every statement is prepared on first use and cached per
  connection, and concurrent queries share a connection without waiting for each other's
  round trips.
* **A routing pool** that sends shared queries to idle connections, or pipelines them onto
  the one with the least expected work. See [Connection pool](guide/pool.md).
* **Template strings**: `t"... WHERE id = {person_id}"` binds values as parameters, never as
  SQL text. See [Template strings](guide/queries.md#template-strings).
* **Bulk writes, transactions and TLS**: binary `COPY`, `insert_many` and `execute_many`;
  savepoints and isolation levels; libpq's `sslmode` options.
