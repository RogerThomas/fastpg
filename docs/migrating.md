# Moving from asyncpg or psycopg

This page shows how common asyncpg and psycopg 3 calls look in fastpg.
For psycopg, it uses the async API: `psycopg.AsyncConnection`, and `psycopg_pool` for pools.

## The calls

| asyncpg | psycopg | fastpg |
|---|---|---|
| `await asyncpg.connect(dsn)` | `await AsyncConnection.connect(dsn)` | `await fastpg.connect(dsn)` |
| `asyncpg.create_pool(dsn)` | `AsyncConnectionPool(dsn)` | `await fastpg.create_pool(dsn)` |
| `pool.acquire()` | `pool.connection()` | `pool.acquire()` |
| `conn.fetch(query, *args)` | `cur.fetchall()` | `conn.select_many(query, *args)` |
| `conn.fetchrow(query, *args)` | `cur.fetchone()` | `conn.select_one_or_none(query, *args)` |
| `conn.fetchval(query, *args)` | `(await cur.fetchone())[0]` | `(await conn.select_one(query, *args))[0]` |
| `conn.execute(query, *args)` | `conn.execute(query, params)` | `conn.execute(query, *args)` |
| `conn.execute(script)` | `conn.execute(script)` | `conn.execute_script(script)` |
| `conn.executemany(query, rows)` | `cur.executemany(query, rows)` | `conn.execute_many(query, rows)` |
| `conn.copy_records_to_table(table, records=rows)` | `cur.copy("COPY ... FROM STDIN")` | `conn.copy_in(table, rows)` |
| `conn.transaction()` | `conn.transaction()` | `conn.transaction()` |

In the psycopg column, `cur` is a cursor: `conn.cursor()`, or what `conn.execute()` returns.
fastpg's pool has the same query methods as a connection, as asyncpg's pool does.

The examples below show only the body of `main()`, as on the other pages, with these imports:

```python
import asyncpg
import msgspec
import psycopg
from psycopg.rows import class_row
from psycopg_pool import AsyncConnectionPool

import fastpg
```

## Connecting and pools

```python
dsn = "postgresql://user:pass@localhost/db"

# asyncpg
conn = await asyncpg.connect(dsn)
await conn.close()

async with asyncpg.create_pool(dsn) as pool:
    async with pool.acquire() as conn:
        print(await conn.fetchval("SELECT 1"))  # 1

# psycopg
async with await psycopg.AsyncConnection.connect(dsn) as conn:
    pass

async with AsyncConnectionPool(dsn) as pool:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT 1")
        print(await cur.fetchone())  # (1,)

# fastpg
async with await fastpg.connect(dsn) as conn:
    pass

async with await fastpg.create_pool(dsn) as pool:
    print(await pool.select_one("SELECT 1"))  # (1,)
    async with pool.acquire() as conn:
        print(await conn.select_one("SELECT 1"))  # (1,)
```

`fastpg.connect()` takes only the DSN and `statement_cache_size=`.
Put the host, user and other settings in the DSN, or in the `PG*` environment variables.
See [Getting started](guide/getting-started.md#settings-from-the-environment).

## Queries

```python
# asyncpg
rows = await conn.fetch("SELECT id, age FROM people WHERE id <= $1 ORDER BY id", 2)
print(rows)  # [<Record id=1 age=19>, <Record id=2 age=20>]
print(await conn.fetchval("SELECT count(*) FROM people"))  # 100000
print(await conn.execute("UPDATE people SET age = age WHERE id <= $1", 2))  # UPDATE 2

# psycopg
cur = await conn.execute("SELECT id, age FROM people WHERE id <= %s ORDER BY id", (2,))
print(await cur.fetchall())  # [(1, 19), (2, 20)]
cur = await conn.execute("UPDATE people SET age = age WHERE id <= %s", (2,))
print(cur.rowcount)  # 2

# fastpg
rows = await conn.select_many("SELECT id, age FROM people WHERE id <= $1 ORDER BY id", 2)
print(rows)  # [(1, 19), (2, 20)]
print((await conn.select_one("SELECT count(*) FROM people"))[0])  # 100000
print(await conn.execute("UPDATE people SET age = age WHERE id <= $1", 2))  # 2
```

fastpg's `execute()` returns the number of rows changed.
See [Queries](guide/queries.md).

## Parameters

| | asyncpg | psycopg | fastpg |
|---|---|---|---|
| by position | `$1`, values as arguments | `%s`, values in a tuple | `$1`, values as arguments |
| by name | | `%(name)s`, values in a dict | `:name`, values in `params=` |
| t-strings (Python 3.14) | | `t"... {value}"` | `t"... {value}"` |

```python
# psycopg
query = "SELECT name FROM people WHERE id = %(id)s AND age >= %(min_age)s"
cur = await conn.execute(query, {"id": 42, "min_age": 30})

# fastpg
query = "SELECT name FROM people WHERE id = :id AND age >= :min_age"
row = await conn.select_one(query, params={"id": 42, "min_age": 30})
```

`params=` also takes a msgspec Struct, a dataclass or a pydantic model.
See [Passing values](guide/queries.md#passing-values).

## Rows as objects

asyncpg returns `Record`s, and psycopg uses row factories.
fastpg returns tuples, or what you pass as `into=`.

| rows as | asyncpg | psycopg | fastpg |
|---|---|---|---|
| tuples | `tuple(record)` | the default | the default |
| dicts | `dict(record)` | `row_factory=dict_row` | `into=dict` |
| your class | `Person(**record)` | `row_factory=class_row(Person)` | `into=Person` |

`into=` takes a msgspec Struct, a `NamedTuple`, a pydantic model or `dict`.

```python
class Person(msgspec.Struct):
    name: str
    age: int


# asyncpg
record = await conn.fetchrow("SELECT name, age FROM people WHERE id = $1", 42)
print(Person(**record))  # Person(name='person-42', age=60)

# psycopg
cur = conn.cursor(row_factory=class_row(Person))
await cur.execute("SELECT name, age FROM people WHERE id = %s", (42,))
print(await cur.fetchone())  # Person(name='person-42', age=60)

# fastpg
query = "SELECT name, age FROM people WHERE id = $1"
print(await conn.select_one(query, 42, into=Person))  # Person(name='person-42', age=60)
```

See [Structs](guide/structs.md) and [pydantic models](guide/pydantic.md).

## Transactions

```python
# asyncpg
async with conn.transaction():
    await conn.execute("INSERT INTO users VALUES ($1)", "a@example.com")

# psycopg
async with conn.transaction():
    await conn.execute("INSERT INTO users VALUES (%s)", ("a@example.com",))

# fastpg
async with conn.transaction() as tx:
    await tx.execute("INSERT INTO users VALUES ($1)", "a@example.com")
```

In fastpg, run the queries on `tx`.
`isolation=`, `readonly=` and `deferrable=` work as in asyncpg.
For a nested transaction, use `tx.savepoint()`: calling `conn.transaction()` again raises
`InterfaceError`.
See [Transactions](guide/transactions.md).

## Bulk writes and COPY

```python
rows = [("a", 1), ("b", 2)]

# asyncpg
await conn.executemany("INSERT INTO scores VALUES ($1, $2)", rows)
await conn.copy_records_to_table("scores", records=rows)

# psycopg
async with conn.cursor() as cur:
    await cur.executemany("INSERT INTO scores VALUES (%s, %s)", rows)
    async with cur.copy("COPY scores FROM STDIN") as copy:
        for row in rows:
            await copy.write_row(row)

# fastpg
await conn.execute_many("INSERT INTO scores VALUES ($1, $2)", rows)
await conn.copy_in("scores", rows)
```

Both fastpg methods return the number of rows written.
`copy_in` also takes Structs, dicts and pydantic models.
See [Bulk writes](guide/writes.md).

## Errors

| what went wrong | asyncpg | psycopg | fastpg |
|---|---|---|---|
| any error from the server | `PostgresError` | `psycopg.DatabaseError` | `DatabaseError` |
| duplicate key | `UniqueViolationError` | `errors.UniqueViolation` | `UniqueViolationError` |
| missing foreign key | `ForeignKeyViolationError` | `errors.ForeignKeyViolation` | `ForeignKeyViolationError` |
| NULL in a `NOT NULL` column | `NotNullViolationError` | `errors.NotNullViolation` | `NotNullViolationError` |
| serialization failure | `SerializationError` | `errors.SerializationFailure` | `SerializationError` |
| deadlock | `DeadlockDetectedError` | `errors.DeadlockDetected` | `DeadlockDetectedError` |
| bad SQL | `PostgresSyntaxError` | `errors.SyntaxError` | `SQLSyntaxError` |
| missing table | `UndefinedTableError` | `errors.UndefinedTable` | `UndefinedTableError` |
| query cancelled | `QueryCanceledError` | `errors.QueryCanceled` | `QueryCanceledError` |
| a value of the wrong type | `DataError` | | `ParameterError` |
| wrong password | `InvalidPasswordError` | `psycopg.OperationalError` | `AuthenticationError` |
| can't connect | `OSError` | `psycopg.OperationalError` | `ConnectError` |
| connection closed | `InterfaceError` | `psycopg.OperationalError` | `ConnectionClosedError` |

asyncpg's errors are in `asyncpg`, psycopg's `errors.` ones in `psycopg.errors`, and
fastpg's in `fastpg`.
To handle "the database is unreachable" in fastpg, catch `OperationalError`.
See [Errors](guide/errors.md).

## Things that work differently

* **Query strings must be string literals or t-strings.** They are typed `LiteralString`,
  so a type checker flags an f-string query.
  See [SQL built at runtime](guide/queries.md#sql-built-at-runtime).
* **`select_one` raises when there is no row**, with `NoRowsError`.
  It and `select_one_or_none` raise `TooManyRowsError` for more than one row.
  asyncpg's `fetchrow` and psycopg's `fetchone` return `None` or the first row instead.
* **`execute()` runs one statement.** For several `;`-separated statements, use
  `execute_script()`.
* **There's no transaction unless you start one.** psycopg opens a transaction on the first
  query unless you set `autocommit=True`.
  fastpg runs each query on its own, like asyncpg.
* **A connection runs many queries at once.** asyncpg raises `InterfaceError` and psycopg
  runs them one after another; fastpg sends them together.
  They may not run in the order you sent them.
  The pool shares its connections the same way, so don't run `SET` on the pool: use
  `pool.acquire()`.
  See [Connection pool](guide/pool.md).
* **Prepared statements can't be turned off.** asyncpg's `statement_cache_size=0` and
  psycopg's `prepare_threshold=None` have no match.
  With pgbouncer in transaction mode, use pgbouncer 1.21 or later with
  `max_prepared_statements` above 0.
* **TLS settings go in the DSN.** There's no `ssl=` argument as in asyncpg.
  The default is `sslmode=prefer`, as in asyncpg and libpq: TLS if the server offers it,
  without checking the server's certificate.
  Use `sslmode=verify-full` to check it.
  `sslmode=allow` isn't supported.
  See [TLS](guide/tls.md).
* **`json` and `jsonb` come back parsed**, and you can pass a dict or list as a value.
  asyncpg returns and takes a `str`, and psycopg needs `Json()` or `Jsonb()` around the
  value.
* **No `timeout=` argument.** Wrap the call in `asyncio.timeout()`.
  fastpg cancels the query on the server too, when nothing else is running on its connection.
* **Some types can't be read yet**, such as `interval`, which asyncpg and psycopg return as
  a `timedelta`.
  See [Types](guide/types.md#types-fastpg-doesnt-support).

## What fastpg doesn't do

There's no `LISTEN`/`NOTIFY`, no `COPY ... TO`, and no cursors: `select_many` reads every
row at once.
There are no custom type codecs, as with asyncpg's `set_type_codec()` or psycopg's adapters.
See [Limitations](limitations.md) for the full list.
