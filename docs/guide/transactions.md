# Transactions

`conn.transaction()` starts a transaction.
It commits when the block ends, and rolls back if the block raises.

```python
await conn.execute_script("CREATE TEMP TABLE users (email text PRIMARY KEY)")

async with conn.transaction() as tx:
    await tx.execute("INSERT INTO users VALUES ($1)", "a@example.com")
    await tx.execute("INSERT INTO users VALUES ($1)", "b@example.com")

print(await conn.select_many("SELECT email FROM users ORDER BY email"))
# [('a@example.com',), ('b@example.com',)]
```

`tx` has all the query methods of a connection, bulk writes included.
With a pool, use `pool.transaction()`: it takes the same options.

## Options

* `isolation=`: `"read_uncommitted"`, `"read_committed"`, `"repeatable_read"` or
  `"serializable"`.
  The default is the server's.
* `readonly=True` for a read-only transaction.
* `deferrable=True` for a deferrable one.

```python
async with conn.transaction(isolation="serializable", readonly=True) as tx:
    print(await tx.select_one("SELECT count(*) FROM users"))  # (2,)
```

## Savepoints

When a statement fails, PostgreSQL aborts the whole transaction.
To catch an error and carry on, wrap the statement in `tx.savepoint()`:

```python
async with conn.transaction() as tx:
    try:
        async with tx.savepoint():
            await tx.execute("INSERT INTO users VALUES ($1)", "a@example.com")
    except fastpg.UniqueViolationError:
        print("already registered")
    await tx.execute("INSERT INTO users VALUES ($1)", "c@example.com")
```

The savepoint is rolled back and the error is raised again; the transaction carries on.
Savepoints can be nested.

## Good to know

* Retry the whole transaction on `SerializationError` or `DeadlockDetectedError`.
* A connection runs one transaction at a time.
  Don't share a connection that has an open transaction with other tasks: their queries
  would run inside it.
  `pool.transaction()` gives each transaction its own connection.
* If a table changes while a transaction uses it, a query can fail with
  `NotSupportedError`. Retry the transaction.
