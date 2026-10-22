<h1>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/RogerThomas/fastpg/main/docs/assets/wordmark-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/RogerThomas/fastpg/main/docs/assets/wordmark-light.svg">
    <img alt="fastpg" src="https://raw.githubusercontent.com/RogerThomas/fastpg/main/docs/assets/wordmark-light.svg" height="56">
  </picture>
</h1>

<a href="https://app.codspeed.io/RogerThomas/fastpg?utm_source=badge"><img src="https://img.shields.io/endpoint?url=https://codspeed.io/badge.json" alt="CodSpeed"/></a>

An async PostgreSQL client for Python 3.14, written in Rust on [xitca-postgres] and [PyO3].
It decodes rows from PostgreSQL's binary format straight into [msgspec] Structs, with no
intermediate tuples or dicts, and runs the whole protocol on the asyncio event loop's thread. In CI benchmarks against asyncpg, psycopg 3 and psqlpy, it does 2-3x the
throughput of the fastest of them on primary-key lookups, pages of rows into Structs, and
single-row writes.

Status: alpha. macOS and Linux only.

## Install

```sh
uv add fastpg                  # or: pip install fastpg
```

msgspec comes with it: Structs are the recommended row type, and the fastest. pydantic v2
models work too (`uv add 'fastpg[pydantic]'`, see [pydantic]).

Python 3.14.3 or later, on Linux or macOS.

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

* **Rows into Structs.** `into=Person` builds msgspec Structs (also NamedTuples, dicts, or
  pydantic models). Values are decoded in Rust and passed straight to the Struct's own
  `__init__`; columns are bound to fields and type-checked once per statement. A type that
  needs validation (constraints, enums) is validated by msgspec, never skipped. See
  [Structs].
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
other" is the fastest of asyncpg, psycopg 3 and psqlpy, each through its own pool.

| workload | fastpg | best other | vs best other |
|---|---:|---:|---:|
| Primary-key lookup into a Struct, 16 tasks | 39,774/s | 17,144/s asyncpg, no reset² | **2.32x** |
| Primary-key lookup, 64 tasks on 16 connections¹ | 54,019/s | 17,577/s asyncpg, no reset² | **3.07x** |
| Page of 20 rows into Structs, 16 tasks | 19,002/s | 9,094/s asyncpg, no reset² | **2.09x** |
| 1,000 rows into Structs, 4 tasks | 1.03M rows/s | 476k rows/s asyncpg | **2.15x** |
| Single-row INSERT, 16 tasks | 35,860/s | 16,087/s asyncpg, no reset² | **2.23x** |
| Binary COPY, 5,000 rows | 1.16M rows/s | 823k rows/s psycopg | **1.41x** |

¹ fastpg pipelines here; with pipelining off it does 37,599/s (2.14x).

² asyncpg with its pool's reset query turned off (a no-op `reset=` in `create_pool`). By
default its pool resets the session every time a connection goes back to it, one extra round
trip per query. fastpg's pool queries aren't reset either (they mustn't change session state),
nor are psycopg's or psqlpy's, so this is the like-for-like asyncpg.

These are client-side numbers on one shared GitHub runner (4 vCPUs), with PostgreSQL on the
same VM. Each driver uses its own pool of 16 with its recommended settings, every typed
workload returns the same msgspec Struct from every driver, and every result is checked.
fastpg's narrowest lead is a six-round-trip transaction: 1.27x asyncpg, which resets the session
after it as fastpg's `pool.transaction()` does (in the COMMIT's round trip), and 1.13x the
configurations that don't. Over a real network the round trip shrinks single-query gaps. All workloads, latency and caveats:
[Benchmarks] and [Methodology].

fastpg's own CPU per query (each row type, the query forms and the bulk writes) is also
tracked on every pull request by [CodSpeed](https://app.codspeed.io/RogerThomas/fastpg),
under CPU simulation: `benchmarks/`, run by `.github/workflows/codspeed.yml`.

## Documentation

* [Getting started]
* Guides: [Queries], [Structs], [pydantic], [Connection pool], [Writes], [Transactions],
  [TLS], [Errors]
* [Limitations]: notably, arrays, `interval` and ranges aren't decoded yet, and pgbouncer's
  transaction pooling mode isn't supported.
* [Architecture] and its [design decisions]

`task docs` builds and serves the documentation site locally.

fastpg's own client-side CPU per query (tuples, Structs, dicts, pydantic, msgpack/json
strategies, t-strings, named parameters and the bulk writes) is tracked on every pull request
with [CodSpeed](https://app.codspeed.io/RogerThomas/fastpg): the pytest-codspeed benchmarks in
`benchmarks/`, run by `.github/workflows/codspeed.yml` under CPU simulation. Locally, against
`task db-up`: `codspeed run --mode simulation -- uv run pytest benchmarks --codspeed` (or
`uv run pytest benchmarks` to just check they pass).

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
