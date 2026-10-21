<h1>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/RogerThomas/fastpg/main/docs/assets/wordmark-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/RogerThomas/fastpg/main/docs/assets/wordmark-light.svg">
    <img alt="fastpg" src="https://raw.githubusercontent.com/RogerThomas/fastpg/main/docs/assets/wordmark-light.svg" height="56">
  </picture>
</h1>

An async PostgreSQL client for Python 3.14, written in Rust on [xitca-postgres] and [PyO3].
It decodes rows from PostgreSQL's binary format straight into [msgspec] Structs (or pydantic
models), with no intermediate tuples or dicts, and runs the whole protocol on the asyncio
event loop's thread. In CI benchmarks against asyncpg, psycopg 3 and psqlpy, it does 2-3x the
throughput of the fastest of them on primary-key lookups, pages of rows into Structs, and
single-row writes.

Status: alpha. macOS and Linux only.

## Install

```sh
uv add fastpg                  # or: pip install fastpg
uv add 'fastpg[pydantic]'      # with pydantic v2 support
```

Python 3.14.3 or later. Not on PyPI yet: until the first release, build from source, which
needs a Rust toolchain: `uv add git+https://github.com/RogerThomas/fastpg`.

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

        first, last = 1, 3
        people = await pool.select_many(
            t"SELECT id, name, email FROM people WHERE id BETWEEN {first} AND {last}",
            into=Person,
        )
        print(len(people))  # 3


asyncio.run(main())
```

The DSN is the repository's example database (`task db-up`). For a script that runs one
query at a time, `await fastpg.connect(dsn)` opens a single connection with the same methods.

## Features

* **Rows into Structs.** `into=Person` builds msgspec Structs, pydantic models, NamedTuples
  or dicts. Values are decoded in Rust and passed straight to the Struct's own `__init__`;
  columns are bound to fields and type-checked once per statement. A type that needs
  validation (constraints, enums, validators) is validated by msgspec or pydantic, never
  skipped. See [Structs] and [pydantic].
* **On the event loop.** fastpg starts no threads: the protocol runs on the event loop's
  thread, so a query costs no thread hand-off. Statements are prepared on first use and cached per connection.
* **A pool with no `acquire()` for single statements.** `pool.select_one(...)` goes to an
  idle connection or, when all are busy, pipelines onto the one with the least queued work.
  `pool.acquire()` and `pool.transaction()` give you a connection of your own, reset on
  release. See [Connection pool].
* **Bulk writes.** `copy_in` (binary COPY), `insert_many` and `execute_many` take Structs,
  models, mappings or tuples, and are all-or-nothing. See [Writes].
* **Safe queries.** `$1` parameters, `:name` with `params=`, or t-strings whose values are
  always bound, never spliced into SQL. Server errors map to exception classes by SQLSTATE
  (`UniqueViolationError`, ...). Typed for `pyright --strict`. See [Queries] and [Errors].

## Benchmarks

Throughput in [run 39671855945] of the [Benchmarks workflow] (`main`, 2026-10-21). "Best
other" is the fastest of asyncpg, psycopg 3 and psqlpy.

| workload | fastpg | best other | vs best other | vs asyncpg |
|---|---:|---:|---:|---:|
| Primary-key lookup into a Struct, 16 tasks | 39,774/s | 17,144/s asyncpg² | **2.32x** | 3.60x |
| Primary-key lookup, 64 tasks on 16 connections¹ | 54,019/s | 17,577/s asyncpg² | **3.07x** | 4.83x |
| Page of 20 rows into Structs, 16 tasks | 19,002/s | 9,094/s asyncpg² | **2.09x** | 2.70x |
| 1,000 rows into Structs, 4 tasks | 1.03M rows/s | 476k rows/s asyncpg | **2.15x** | 2.15x |
| Single-row INSERT, 16 tasks | 35,860/s | 16,087/s asyncpg² | **2.23x** | 3.38x |
| Binary COPY, 5,000 rows | 1.16M rows/s | 823k rows/s psycopg | **1.41x** | 2.65x |

¹ fastpg pipelines here; with pipelining off it does 37,599/s (2.14x).

² With its per-release reset query turned off, which saves it a round trip.

These are client-side numbers on one shared GitHub runner (4 vCPUs), with PostgreSQL on the
same VM. Each driver uses its own pool of 16 with its recommended settings, every typed
workload returns the same msgspec Struct from every driver, and every result is checked.
fastpg's narrowest lead is a six-round-trip transaction (1.13x), and over a real network the
round trip shrinks single-query latency gaps. All workloads, latency and caveats:
[Benchmarks] and [Methodology].

## Documentation

* [Getting started]
* Guides: [Queries], [Structs], [pydantic], [Connection pool], [Writes], [Transactions],
  [TLS], [Errors]
* [Limitations]: notably, arrays, `interval` and ranges aren't decoded yet, and pgbouncer's
  transaction pooling mode isn't supported.
* [Architecture] and its [design decisions]

`task docs` builds and serves the documentation site locally.

## Development

Needs Docker, [uv], [Task] and a Rust toolchain.

```sh
task build    # compile the extension into .venv (uv sync)
task test     # cargo test + pytest; starts its own PostgreSQL containers
task check    # ruff, pyright (strict), clippy and the other prek hooks
task db-up    # the example database on 127.0.0.1:55432, seeded with 100,000 people
task --list   # everything else: benchmarks, the TLS server, docs
```

MIT licensed.

[xitca-postgres]: https://github.com/HFQR/xitca-web/tree/main/postgres
[PyO3]: https://pyo3.rs
[msgspec]: https://msgspec.dev/
[uv]: https://docs.astral.sh/uv/
[Task]: https://taskfile.dev
[run 39671855945]: https://github.com/RogerThomas/fastpg/actions/runs/39671855945
[Benchmarks workflow]: https://github.com/RogerThomas/fastpg/actions/workflows/benchmarks.yml
[Getting started]: https://github.com/RogerThomas/fastpg/blob/main/docs/guide/getting-started.md
[Queries]: https://github.com/RogerThomas/fastpg/blob/main/docs/guide/queries.md
[Structs]: https://github.com/RogerThomas/fastpg/blob/main/docs/guide/structs.md
[pydantic]: https://github.com/RogerThomas/fastpg/blob/main/docs/guide/pydantic.md
[Connection pool]: https://github.com/RogerThomas/fastpg/blob/main/docs/guide/pool.md
[Writes]: https://github.com/RogerThomas/fastpg/blob/main/docs/guide/writes.md
[Transactions]: https://github.com/RogerThomas/fastpg/blob/main/docs/guide/transactions.md
[TLS]: https://github.com/RogerThomas/fastpg/blob/main/docs/guide/tls.md
[Errors]: https://github.com/RogerThomas/fastpg/blob/main/docs/guide/errors.md
[Limitations]: https://github.com/RogerThomas/fastpg/blob/main/docs/limitations.md
[Architecture]: https://github.com/RogerThomas/fastpg/blob/main/docs/architecture.md
[design decisions]: https://github.com/RogerThomas/fastpg/blob/main/adr/README.md
[Benchmarks]: https://github.com/RogerThomas/fastpg/blob/main/docs/performance.md
[Methodology]: https://github.com/RogerThomas/fastpg/blob/main/docs/benchmarks.md
