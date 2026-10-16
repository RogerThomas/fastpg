# Transactions

`conn.transaction()` runs `BEGIN` on entry, `COMMIT` when the block exits normally, and
`ROLLBACK` when it raises, cancellation included.

```python
await conn.execute_script("CREATE TEMP TABLE users (email text PRIMARY KEY)")
await conn.execute("INSERT INTO users VALUES ($1)", "a@example.com")

async with conn.transaction(isolation="serializable") as tx:
    try:
        async with tx.savepoint():
            await tx.execute("INSERT INTO users VALUES ($1)", "a@example.com")
    except fastpg.UniqueViolationError:
        print("already registered")
    await tx.execute("INSERT INTO users VALUES ($1)", "b@example.com")

print(await conn.select_many("SELECT email FROM users ORDER BY email"))
# [('a@example.com',), ('b@example.com',)]
```

`tx` is a `Transaction`, with every query method of a `Connection`, bulk writes included.

## Options

* `isolation`: `"read_uncommitted"`, `"read_committed"`, `"repeatable_read"` or
  `"serializable"`; `None` (the default) keeps the server's default.
* `readonly=True` starts a `READ ONLY` transaction; `deferrable=True` a `DEFERRABLE` one.
* `tx.savepoint()` nests to any depth: it's released when its block exits normally and
  rolled back when it raises, and the exception propagates.
* `pool.transaction()` takes the same options, on a connection it has to itself for the block.

## Gotchas

* After a statement fails, PostgreSQL aborts the transaction. If the block catches the error
  and carries on, the commit rolls back and raises a `DatabaseError` with SQLSTATE `25P02`.
  Wrap statements whose errors you expect in `tx.savepoint()`.
* `SerializationError` (`40001`) and `DeadlockDetectedError` (`40P01`), both
  `fastpg.errors.TransactionError`, mean the transaction should be retried from the start.
* A connection has one transaction at a time; starting a second raises `InterfaceError`.
* Outside a transaction, a cached statement whose result type changed (DDL such as
  `ALTER TABLE ... ADD COLUMN` under `SELECT *`) is re-prepared transparently. Inside one,
  the server's error has already aborted the transaction, so the query raises
  `NotSupportedError` ("cached plan must not change result type") instead: retry the
  transaction, which prepares the statement afresh.
* Bulk writes inside a transaction use their own savepoint, so a failed one doesn't abort it.

!!! warning "Keep a connection with an open transaction to one task"

    Queries that other tasks send through the same `Connection` are pipelined into the same
    session, so they run inside the transaction. `pool.transaction()` and `pool.acquire()`
    give the transaction a connection of its own.
