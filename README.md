<h1>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/wordmark-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="docs/assets/wordmark-light.svg">
    <img alt="fastpg" src="docs/assets/wordmark-light.svg" height="56">
  </picture>
</h1>

Docs: `task docs` (builds and serves the documentation site).

An async PostgreSQL client for Python 3.14, written in Rust on top of
[xitca-postgres](https://github.com/HFQR/xitca-web/tree/main/postgres) and
[PyO3](https://pyo3.rs). Its main purpose is to decode rows **straight into
[`msgspec.Struct`](https://jcristharif.com/msgspec/) instances**:

```python
import msgspec, fastpg


class Person(msgspec.Struct):
    name: str
    age: int


async with await fastpg.connect("postgresql://fastpg:fastpg@127.0.0.1:55432/fastpg") as conn:
    person = await conn.select_one(
        "SELECT name, age FROM people WHERE id = $1",
        42,
        into=Person,
    )
```

* Statements are prepared on first use and cached per connection (LRU, keyed by SQL text).
* Results always use PostgreSQL's **binary** format, decoded in Rust.
* `into=Person` builds msgspec Structs **or pydantic models** without intermediate dicts or
  tuples. Column/field binding and type checks run once per (statement, type), not once per
  row.
* Concurrent queries from many tasks are **pipelined** over one connection.
* Typed API: the package passes `pyright --strict`, and the native module ships a `.pyi` stub.

> Status: an early but tested prototype. See [Limitations](#limitations) and
> the [architecture decision records](adr/README.md) for the choices made along the way.

## Quick start

Requires Docker, [uv](https://docs.astral.sh/uv/), a Rust toolchain (stable ≥ 1.88) and,
optionally, [Task](https://taskfile.dev).

```sh
task db-up            # PostgreSQL 18.6 on 127.0.0.1:55432, schema + 100k seeded rows
task build            # uv sync: compiles the Rust extension (release) into .venv
task test             # Rust unit tests + pytest suite; starts its own PostgreSQL containers
task example          # ./examples/basic.py
```

Without Task: `uv sync --all-groups`, `uv run pytest`, and `docker compose up -d --wait postgres`
for the example.
To test against a server you already have, see `task test-against` under
[Development](#development).

To run everything inside Docker, with no local Rust or Python needed:

```sh
task test-docker      # builds fastpg on Linux, runs both test suites against the compose DB
```

## API

```python
conn = await fastpg.connect(dsn, statement_cache_size=256)  # -> fastpg.Connection

await conn.select_one(query, *args, into=T)  # -> T          (exactly one row)
await conn.select_one_or_none(query, *args, into=T)  # -> T | None   (zero or one row)
await conn.select_many(query, *args, into=T)  # -> list[T]
await conn.select_one(query, *args)  # -> tuple[Any, ...] (no into=)
await conn.execute(query, *args)  # -> int, rows affected
await conn.execute_script("CREATE ...; INSERT ...")  # simple protocol, no params

await conn.select_one(
    "... WHERE id = :id", params=sel, into=T
)  # named: Struct/model/dataclass/dict
await conn.select_one(t"SELECT ... WHERE id = {pid}", into=T)  # t-string: values bound as $n

await conn.execute_many(query, rows)  # -> int; one statement, many param rows
await conn.insert_many("people", rows)  # -> int; Structs / models / mappings / tuples
await conn.copy_in("people", rows)  # -> int; binary COPY, fastest for bulk loads

async with conn.transaction() as tx:  # commit on success, rollback on exception
    await tx.execute(...)  # same query methods as conn; nest with tx.savepoint()

pool = await fastpg.create_pool(dsn, max_size=16)  # -> fastpg.Pool
await pool.select_one(query, *args, into=T)  # shared: routed + pipelined, no acquire
async with pool.acquire() as conn:
    ...  # exclusive checkout (session state)
async with pool.transaction() as tx:
    ...  # exclusive, transactional
await pool.copy_in("people", rows)  # bulk writes run on an exclusive checkout
await conn.close()  # or: async with await connect(...)
```

Parameters use PostgreSQL's `$1, $2, …` placeholders. Each one is encoded for the type the
server declared for it, and mismatches raise `ParameterError` before anything is sent:

| Python | PostgreSQL |
|---|---|
| `None` | NULL (any type) |
| `bool` | bool |
| `int` | int2/int4/int8/oid (range-checked), float4/8, numeric |
| `float` | float4/float8, numeric |
| `str` | text, varchar, bpchar, name, citext, enums, json/jsonb, uuid, numeric |
| `bytes`, `bytearray`, `memoryview` | bytea |
| `uuid.UUID`, `decimal.Decimal` | uuid, numeric |
| `datetime` naive / aware | timestamp / timestamptz (mixing them is an error) |
| `date`, `time` | date, time |
| `dict`, `list` | json/jsonb (encoded with msgspec) |
| `enum.Enum` | its `.value` |

### TLS

`sslmode` and friends work as in libpq:

```python
await fastpg.connect(
    "postgresql://u:pw@db.example:5432/app?sslmode=verify-full&sslrootcert=/etc/ca.pem"
)
```

* `sslmode`: `disable`, `prefer` (the default: TLS when the server offers it, unverified),
  `require` (encrypted, unverified), `verify-ca` (trusted chain), `verify-full` (trusted
  chain + host name). `allow` isn't supported.
* `sslrootcert`: a PEM file, or `system`. `verify-*` without it uses the system's trusted
  roots (Mozilla's if the system has none). For Amazon RDS/Aurora, pass the RDS CA bundle.
* `sslnegotiation=direct` (PostgreSQL 17+), `sslcert`/`sslkey` (client certificates),
  `sslsni=0`.
* `channel_binding`: `prefer` (the default: SCRAM over TLS is channel-bound when the server
  offers it), `require` (only a channel-bound SCRAM login over TLS), `disable`.
* Unix-socket connections never use TLS (as in libpq).

Details, the certificate rules and limitations: [notes/tls.md](notes/tls.md).

### Connection pool

PostgreSQL executes one connection's queries strictly in order. So a single shared pipelined
connection collapses under mixed workloads: with 1% of requests being 20 ms queries,
fast-query p99 hit 776 ms at 5k req/s. A plain asyncpg-style pool (one query per
connection) is robust but wastes pipelining. `fastpg.Pool` combines the two:

* **Shared queries** (`pool.select_*`, `execute`, `execute_script`) don't acquire anything.
  - Each goes to an idle connection when there is one.
  - Otherwise the pool opens a new connection (up to `max_size`), without blocking the query
    on the connect.
  - Otherwise it pipelines onto the connection with the least expected queued work, at most
    `max_inflight_per_conn` (default 8) deep.
  - Otherwise it waits FIFO for the first free slot.
  - "Expected work" is a moving average of each SQL text's observed runtime, so a connection
    stuck behind a known-slow query is avoided.
* **Exclusive use:**
  - `pool.acquire()` and `pool.transaction()` (isolation, read-only, deferrable, nested
    `savepoint()`) check out an idle connection.
  - On release, session state is reset in one round trip; prepared statements are kept.
  - Bulk writes on the pool use an exclusive checkout, so a long COPY never delays shared
    queries.
* **Maintenance:** dead connections (server restart, `pg_terminate_backend`) are replaced
  lazily, nothing is retried, idle connections above `min_size` are closed after `max_idle`,
  and connections are recycled after `max_lifetime`. `pool.stats()` reports sizes, in-flight
  queries and waiters.

Linux, 1% slow (20 ms) queries, fast-query p99 / p99.9 (measured on the earlier threaded I/O
path; the current path's fast-query p50 in the same benchmark is 43–59 µs, against 84–102 µs):

| rate | fastpg pool (16) | asyncpg pool (16) | asyncpg pool (32) |
|---:|---:|---:|---:|
| 10k/s | **208 µs / 334 µs** | 694 µs / 1.2 ms | 694 µs / 1.1 ms |
| 20k/s | **112 µs / 208 µs** | 1.2 ms / 3.1 ms | 2.5 ms / 19.5 ms |

Details, the routing algorithm and the measurements behind the defaults are in
[notes/pool.md](notes/pool.md).

### Queries as t-strings (Python 3.14, PEP 750)

Every query method also accepts a template string. Interpolations become **bound
parameters**, never SQL text, so this is injection-safe by construction:

```python
person = await cur.select_one(
    t"SELECT name, age FROM people WHERE id = {sel.id} AND age >= {sel.min_age}",
    into=Person,
)  # runs: ... WHERE id = $1 AND age >= $2, with (sel.id, sel.min_age)
```

* **Composition:** a nested template is spliced in and its values renumbered.
  `where = t"age >= {n}"`, then `t"SELECT ... WHERE {where}"`.
* **Identifiers:** `fastpg.Identifier("schema", "table")` interpolates a safely quoted name.
  Parameters can't stand for tables or columns.
* **Rejected:** conversions and format specs (`{x!r}`, `{x:.2f}`), and literal `$1` in
  template text.
* **Caching:** the generated SQL is cached per template literal, about 0.15–0.2 µs per call
  (~0.5 µs end to end per query, within noise), and it shares prepared statements with the
  equivalent `$n` query.

Design and edge cases are in [notes/templates.md](notes/templates.md).

### Bulk writes

| method | rows | how | atomicity |
|---|---|---|---|
| `execute_many(query, rows)` | sequences for `$1..$n` | one prepared statement run per row, 1,024 rows pipelined per protocol `Sync` | all-or-nothing |
| `insert_many(table, rows, *, columns=None)` | msgspec Structs (encoded names), pydantic models (aliases), mappings, sequences | multi-row `INSERT … VALUES` (64 rows per statement), quoted identifiers | all-or-nothing |
| `copy_in(table, rows, *, columns=None)` | same as `insert_many` | binary `COPY FROM STDIN`, encoded in Rust and streamed in 4,096-row chunks | all-or-nothing |

Each write runs in its own `BEGIN … COMMIT`. Inside a caller's transaction it uses a
`SAVEPOINT` instead, so a failed or cancelled bulk write leaves the caller's transaction
intact. Errors carry `row_index` (0-based; for `insert_many`, the note gives the failing
64-row range).

100,000 ten-column rows, Linux containers under a one-CPU quota (`--cpus=1`), 2026-10-11
(median of three runs, each the best of three; raw logs in `notes/eval/2026-10-perf-round/`).
The psycopg 3 rows and the event-loop CPU column are from an earlier run, on the threaded
I/O path with no CPU limit (see [Benchmarks](#benchmarks)):

| method | rows/s | event-loop CPU per row (earlier run) |
|---|---:|---:|
| fastpg `copy_in` | **1.25M** | **503 ns** |
| psycopg binary `copy` | 1.30M (earlier run) | 703 ns |
| asyncpg `copy_records_to_table` | 746k | 1,263 ns |
| fastpg `insert_many` | **540k** | 534 ns |
| fastpg `execute_many` | **292k** | 580 ns |
| asyncpg `executemany` | 287k | 1,955 ns |
| psycopg `executemany` | 42k (earlier run) | 23.8 µs |

In the earlier run `execute_many` still sent a protocol Sync per row (117k rows/s). It now
shares one Sync per 1,024 rows, which puts it on par with asyncpg's `executemany` on Linux,
at 3.6x less loop CPU per row than asyncpg on macOS
([notes/perf-writes.md](notes/perf-writes.md)). Details and xitca source references are in
[notes/writes.md](notes/writes.md).

### Decoding into msgspec Structs and pydantic models

`into=` accepts any `msgspec.Struct` subclass or pydantic v2 `BaseModel` subclass
(`pip install fastpg[pydantic]`). The return type follows: `into=Person` gives `Person`. The
`strategy=` argument picks how rows become objects:

| strategy | what happens | validated? |
|---|---|---|
| `"direct"` | Rust decodes each binary value into its Python object and builds the instance itself. **Structs**: `__init__` called through `PyObject_Vectorcall` (positional when the columns match the field order, otherwise keyword arguments using the Struct's own interned field names). **Pydantic models**: built the way `model_construct` does it, natively (a `__dict__` in field order plus the three `__pydantic_*__` slots, no `__init__`). | Column types are checked against field types once per statement; values are already the exact field types. |
| `"msgpack"` (msgspec) | Rust encodes rows as MessagePack maps; a cached `msgspec.msgpack.Decoder(T)` builds them. | fully, by msgspec |
| `"json"` | Rust encodes rows as JSON; a cached `msgspec.json.Decoder(T)` or pydantic-core's `TypeAdapter(T).validate_json` builds them. Both parsers are native, so there are no Python intermediates. | fully, by msgspec / pydantic |
| `"auto"` (default) | `"direct"` when it's provably equivalent to validation, otherwise `"msgpack"` (msgspec) or `"json"` (pydantic) | |

**Direct mode supports** fields of type `bool, int, float, str, bytes, datetime, date, time,
UUID, Decimal, Any`, their `| None` forms, and JSON-shaped fields (`list`, `dict`, nested
Structs or models, …) read from json/jsonb columns, each decoded with a cached
`msgspec.json.Decoder` / `TypeAdapter` for that field.

**Direct is never used when it could change results.** A type falls back (or raises under
`strategy="direct"`) if it has any of:
- msgspec: `Meta` constraints, enums, `Literal`, unions other than `T | None`, `tz=`
  constraints;
- pydantic (decided by walking `__pydantic_core_schema__`): validators of any kind, field
  constraints, `extra="allow"`, private attributes, `model_post_init`, a custom `__init__`,
  `AliasPath`/`AliasChoices`, `default_factory`/`validate_default`, mutable defaults.

Columns are matched by the *encoded* field name (msgspec `rename=`, pydantic `alias`). Fields
with defaults may be missing from the result. Extra columns are ignored unless
`forbid_unknown_fields=True` / `extra="forbid"`. All binding and type checks run once per
(prepared statement, type) and raise before any row is decoded, even for empty results.

> **Why there's no "native pydantic-core API" route:** pydantic-core has no Rust API. It isn't
> published as a crate, and a second compiled copy couldn't share type objects or schemas with
> your installed pydantic. Its fastest native entry point is `validate_json` (jiter, in Rust),
> which the `"json"` strategy feeds directly. See [perf-notes.md](perf-notes.md#pydantic).

### Errors

```
FastPGError (also: Error)
├── InterfaceError → ConnectionClosedError, ParameterError
├── ResultError
│   ├── NoRowsError (also LookupError), TooManyRowsError
│   └── DecodeError → TypeMismatchError, NullValueError, MissingColumnError,
│                     UnknownColumnError, UnsupportedTypeError
└── DatabaseError (.sqlstate, .detail, .hint, .constraint_name, .table_name, …)
    ├── DataError (22), IntegrityError (23) → UniqueViolationError, ForeignKeyViolationError, …
    ├── TransactionError (40) → SerializationError, DeadlockDetectedError
    ├── ProgrammingError (42) → SQLSyntaxError, UndefinedTableError, UndefinedColumnError
    ├── OperationalError (08/53/57) → QueryCanceledError; InternalError (XX)
    └── NotSupportedError (0A)
```

Every DB-API 2.0 (PEP 249) exception name but `Warning` (fastpg never raises one) is
importable from `fastpg`, in PEP 249's hierarchy; `Error` is an alias of `FastPGError`. The
rest of the API isn't DB-API: no stateful cursor, typed `into=`, `$1` parameters.

Examples: a `NULL` in a non-optional field raises `NullValueError` naming the column and the
field. `SELECT name ...` into a Struct with `name: int` raises `TypeMismatchError` before
decoding. A server error leaves the connection usable.

## Architecture

fastpg runs no threads of its own. The asyncio loop thread does everything: socket I/O,
xitca-postgres's protocol work, and decoding into Python objects.

```
 asyncio loop thread (fastpg starts no other)
 ─────────────────────────────────────
 connect(dsn)
   ├─ socket connected in Python (host names via loop.getaddrinfo)
   └─ loop.add_reader / add_writer(socket) → Rust readiness callbacks
 conn.select_one(query, 42, into=Person)
   ├─ params → Rust enums, statement cache lookup
   └─ spawn a query task on the connection's executor, poll it at once
        xitca-postgres: prepare (on cache miss) + bind + execute, binary format, pipelined
 on_readable() (socket ready)
   ├─ poll the woken tasks: xitca's driver parses messages,
   │    query tasks copy rows → raw-bytes arena (memcpy per cell)
   └─ for each completion
        ├─ bind columns ↔ Struct fields (cached)
        ├─ decode cells → PyObjects, vectorcall Person(...)
        └─ future.set_result(...)
```

* **Loop-native I/O.** `native/src/event_loop.rs` has a small per-connection `Executor` that
  polls xitca-postgres's futures (its driver, the connect handshake, each query) on the loop
  thread, and a `LoopSocket` that implements xitca's `AsyncIo` with readiness fed by
  `loop.add_reader` / `add_writer` callbacks instead of tokio's reactor. There is no tokio
  runtime; tokio is only used for its `sync` primitives and `select!`. A query costs no
  thread hand-off or wake-up syscall.
* **Off-thread wakes.** If a task is woken from another thread (e.g. a `__del__` run by the
  garbage collector there), a `Poker` writes one byte to a per-connection socketpair that the
  loop watches with `add_reader`, and the loop runs the task. Normal queries never use it.
* **Threads.** Numeric addresses and unix sockets connect without any thread. A host *name*
  goes through `loop.getaddrinfo`, which runs in the loop's default executor, as in any
  asyncio client.
* **Server-close detection.** The socket is wrapped in `EofWatch`, so a server that closes
  the connection (restart, `pg_terminate_backend`) fails pending queries at once; TLS
  (rustls) sits between the socket and the watch.
* **Streaming decode.** `select_many` hands rows over every 512 rows, so a large result is
  decoded in chunks as it arrives instead of being buffered whole first.
* **Raw row access.** xitca-postgres 0.4 only exposes typed `Row::try_get::<T: FromSql>`, with
  no raw-bytes accessor. fastpg implements `FromSql` for a private `RawCell` type that accepts
  every type and returns the untouched binary value. All wire-protocol work stays in
  xitca-postgres.
* **UUIDs** are built by filling `uuid.UUID`'s two slots directly, the same way the stdlib does
  internally. This skips the pure-Python `__init__`: about 280 ns → about 50 ns per value,
  decoding included (see [perf-notes.md](perf-notes.md)).

Source map: `native/src/event_loop.rs` (executor, `LoopSocket`), `connection.rs` (operations,
pump, drain), `net.rs` (DSN host list, EOF watch), `tls.rs` (TLS), `mailbox.rs` (completion
queue, off-thread `Poker`), `rows.rs` (arena), `decode.rs` (binary → Python), `structs.rs`
(spec/binding/vectorcall), `msgpack.rs`, `params.rs`, `stmt_cache.rs`, `bulk.rs` (bulk
writes); Python: `src/fastpg/_connection.py` (socket connect, loop registration), `_pool.py`,
`_spec.py`, `errors.py`.

## Benchmarks

One CPU (`--cpus 1`), Linux containers, PostgreSQL 18.6, uvloop, rows decoded into a
10-column msgspec Struct:

| | fastpg | asyncpg |
|---|---:|---:|
| Lookups by id, 16 tasks through the pool | 64.9k ops/s | 25.3k ops/s |
| 10,000-row fetch | 1.73M rows/s | 0.71M rows/s |
| CPU per request | 23.5 µs | 34 µs |

Setup, psycopg 3 and psqlpy, caveats and how to reproduce:
[docs/performance.md](docs/performance.md).

## Development

| task | |
|---|---|
| `task db-up` / `task db-down` | start / discard the PostgreSQL container |
| `task build` | rebuild the extension into `.venv` |
| `task test` | `cargo test` + `pytest`, PostgreSQL (plain and TLS) in throwaway containers |
| `task test-against DSN=… [TLS_DSN=… TLS_CA=…] [SEED=1]` | the same against existing servers, starting none; `SEED=1` first loads `docker/initdb/*.sql` into the (empty) database |
| `task check` | ruff, ruff format, pyright (strict), clippy |
| `task bench` / `task bench-docker` | driver benchmark suite (host / inside Linux) |
| `task bench-open-loop` / `task bench-open-loop-docker` | API-like load at fixed request rates |
| `task bench-decode` | loop-thread CPU per row |
| `task test-docker` | full build + tests in Docker |
| `task db-tls-up` / `task test-tls` / `task db-tls-down` | TLS-only PostgreSQL on 55433 and `tests/test_tls.py` against it |

Environment variables:
- `FASTPG_DSN`: the DSN used by tests, benchmarks and the example. Default for benchmarks
  and the example: `postgresql://fastpg:fastpg@127.0.0.1:55432/fastpg`; without it, the tests
  start their own servers in throwaway containers (testcontainers, on ports Docker picks).
- `FASTPG_TLS_DSN`, `FASTPG_TLS_CA`: the TLS-only test server and its CA certificate
  (`task test-tls` sets them). Without them, the tests start that server too, unless
  `FASTPG_DSN` is set: then the TLS-server tests are skipped.

## Limitations

* Unix only (macOS, Linux): the connection's socket and its off-thread wake-up socketpair
  are registered with `loop.add_reader` / `add_writer`, which Windows' default proactor loop
  doesn't offer.
* A connection is bound to the event loop that opened it and must only be used from that
  loop's thread. This isn't checked: a connection used from another loop or thread, or in a
  child process after `fork()` (which shares the parent's socket), misbehaves rather than
  raising. Open connections (and pools) inside each process and each loop.
* A cancelled query is cancelled on the server only when it's alone on its connection:
  pipelined with others, a cancelled 30 s query keeps its connection busy until it
  finishes. Connecting waits indefinitely unless the DSN has libpq's `connect_timeout`, and
  the pool doesn't back off when the database is down.
* Queries pipelined concurrently from *different tasks* on one connection aren't ordered with
  respect to each other: a statement that needs preparing can be overtaken by an already
  prepared one. Queries awaited in sequence by one task are always ordered.
* No LISTEN/NOTIFY. COPY is write-only (`copy_in`); there's no `COPY TO`.
* TLS: no `sslmode=allow`, encrypted client keys or CRLs, and nothing is read from
  `~/.postgresql/` or `PG*` variables; see
  [notes/tls.md](notes/tls.md).
* While a bulk write runs on a connection, other queries on that connection wait. The pool
  avoids this by running bulk writes on an exclusive checkout.
* Not decoded yet: arrays, interval, timetz, ranges, composite types, and ±infinity dates or
  timestamps. Each raises `UnsupportedTypeError` / `DecodeError` with the column name.
* Statements are always named and cached, so this isn't compatible with pgbouncer in
  transaction-pooling mode.
* Free-threaded CPython (3.14t) isn't targeted yet. The extension declares
  `gil_used = true`, as it hasn't been audited for running without the GIL, so importing
  fastpg re-enables the GIL there.
