<h1 id="fastpg" markdown>
![fastpg](assets/wordmark-light.svg#only-light){ width="240" }
![fastpg](assets/wordmark-dark.svg#only-dark){ width="240" }
</h1>

fastpg is an async PostgreSQL client for Python 3.14, written in Rust on
[xitca-postgres](https://github.com/HFQR/xitca-web/tree/main/postgres) and
[PyO3](https://pyo3.rs). It decodes rows from PostgreSQL's binary format straight into
[msgspec](https://msgspec.dev/) Structs, with no intermediate tuples or dicts, and runs the
whole protocol on the asyncio event loop's thread.

```python
import asyncio

import fastpg
import msgspec


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

## Performance

Throughput against the fastest of asyncpg, psycopg 3 and psqlpy, each with its own pool of 16
connections, every driver returning the same msgspec Structs, on a shared GitHub runner
([details and caveats](performance.md)):

| workload | fastpg | best other | vs best other |
|---|---:|---:|---:|
| Primary-key lookup into a Struct, 16 tasks | 39,774/s | 17,144/s | 2.32x |
| Page of 20 rows into Structs, 16 tasks | 19,002/s | 9,094/s | 2.09x |
| 1,000 rows into Structs, 4 tasks | 1.03M rows/s | 476k rows/s | 2.15x |
| Single-row INSERT, 16 tasks | 35,860/s | 16,087/s | 2.23x |
| Binary COPY, 5,000 rows | 1.16M rows/s | 823k rows/s | 1.41x |

## Install

```sh
uv add fastpg                  # or: pip install fastpg
```

msgspec comes with it: Structs are the recommended row type, and the fastest. pydantic v2
models work too (`uv add 'fastpg[pydantic]'`, see [pydantic](guide/pydantic.md)).

Python 3.14.3 or later, on macOS or Linux, from wheels (see
[Getting started](guide/getting-started.md#install)).

## Features

* **Typed rows**: `into=Person` builds msgspec Structs (also NamedTuples, dicts, or pydantic
  v2 models) from the binary values; a type that needs validation is validated, never
  skipped. See [Structs](guide/structs.md).
* **Prepared, binary, on the loop**: statements are prepared on first use and cached per
  connection, and a query costs no thread hand-off.
* **A routing pool**: single statements need no `acquire()`; they go to idle connections, or
  pipeline onto the one with the least queued work. See [Connection pool](guide/pool.md).
* **Bulk writes**: binary `COPY`, `insert_many` and `execute_many`, all-or-nothing. See
  [Writes](guide/writes.md).
* **Safe queries**: `$1` and `:name` parameters, and t-strings whose values are always bound.
  See [Queries](guide/queries.md).
* **Transactions, TLS, errors**: savepoints and isolation levels, libpq's `sslmode` keys, and
  exceptions mapped from SQLSTATE.

fastpg is alpha software; see [Limitations](limitations.md).
