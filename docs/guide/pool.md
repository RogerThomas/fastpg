# Connection pool

Use a pool when many tasks run queries at the same time, as in a web app.
Create it once when your app starts, and share it.

```python
async with await fastpg.create_pool(dsn) as pool:
    row = await pool.select_one("SELECT name, age FROM people WHERE id = $1", 42)
    print(row)  # ('person-42', 60)
```

Call the query methods on the pool itself.
You don't need `acquire()` for a single query.
Each query runs on its own, outside any transaction.

The pool opens more connections as load grows, up to `max_size`.

## Options

Pass these to `create_pool()`:

| option | default | what it does |
|---|---:|---|
| `min_size` | 1 | connections kept open, even when idle |
| `max_size` | 16 | the most connections the pool opens |
| `max_inflight_per_conn` | 8 | the most queries sent to one connection at a time |
| `statement_cache_size` | 256 | prepared statements kept per connection |
| `max_lifetime` | 3600.0 | seconds before an idle connection is replaced (`None`: never) |
| `max_idle` | 300.0 | seconds before an idle connection above `min_size` closes (`None`: never) |

!!! warning "Queries on the pool must not change the session"

    Other callers share the pool's connections, and the pool doesn't reset them between
    queries.
    For `SET`, temporary tables, advisory locks or `LISTEN`, take a connection of your own
    with `acquire()`, below.

## A connection of your own

`pool.acquire()` gives you a connection for the length of the block.
`pool.transaction()` does the same and runs a [transaction](transactions.md) on it.

```python
async with await fastpg.create_pool(dsn) as pool:
    async with pool.acquire() as conn:
        await conn.execute_script("SET statement_timeout = '5s'")
        print(await conn.select_one("SHOW statement_timeout"))  # ('5s',)

    print(await pool.select_one("SHOW statement_timeout"))  # ('0',)
```

When the block ends, the pool rolls back any open transaction and resets the session, so
the `SET` is gone.

## Slow queries

The pool sends each query to an idle connection when it has one.
When every connection is busy, it queues the query on the least busy one.
A slow query delays the queries queued behind it.

So for queries you know are slow:

* run them on `acquire()` or on a separate pool;
* set a `statement_timeout`;
* or pass `max_inflight_per_conn=1` so queries never queue on a busy connection.

## Good to know

* Put `connect_timeout` (in seconds) in the DSN, e.g. `?connect_timeout=10`.
  Without it, connecting to a server that doesn't answer waits forever.
* If you cancel a query, for example with `asyncio.timeout()`, fastpg also cancels it on
  the server when nothing else is running on its connection.
* A lost connection is dropped, and the pool opens new ones as needed.
  Queries running on it fail with `ConnectionClosedError`; fastpg doesn't retry them.
* `pool.close()` lets running queries finish.
  To limit how long it waits, wrap it in `asyncio.timeout()`.
* `pool.stats()` shows how many connections are open, idle and busy.
* A pool belongs to one event loop. Each process needs its own, so allow
  `processes × max_size` connections on the server.
