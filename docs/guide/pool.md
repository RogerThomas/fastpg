# Connection pool

For scripts, jobs, migrations and anything else that runs one query at a time, use a single
connection:

```python
async with await fastpg.connect(dsn) as conn:
    row = await conn.select_one("SELECT name, age FROM people WHERE id = $1", 42)
```

For concurrency, such as a web app serving many requests at once, use a pool. Create it once
at startup and share it everywhere; it spreads queries over its connections and opens more
as load grows:

```python
async with await fastpg.create_pool(dsn) as pool:
    row = await pool.select_one("SELECT name, age FROM people WHERE id = $1", 42)
    print(row)  # ('person-42', 60)
```

A single connection can also be shared by concurrent tasks (their queries are
[pipelined](#head-of-line-blocking)), but a pool also keeps each transaction on its own
connection, stops one slow query from holding up every request, replaces dropped
connections, and spreads load over several PostgreSQL processes.

## Options

| Option | Default | |
|---|---:|---|
| `min_size` | 1 | connections kept open even when idle |
| `max_size` | 16 | upper bound, reached on demand |
| `max_inflight_per_conn` | 8 | queries pipelined per connection when all are busy; 1 disables pipelining |
| `statement_cache_size` | 256 | prepared statements cached per connection |
| `max_lifetime` | 3600.0 | seconds before a connection is replaced once idle (`None`: never) |
| `max_idle` | 300.0 | idle seconds before a connection above `min_size` closes (`None`: never) |

`create_pool` opens `min_size` connections, and raises (closing the others) if any fails.
The pool is bound to the event loop that created it.

## Running queries

Call the query methods on the pool itself; no `acquire()` needed: `pool.select_one`,
`select_one_or_none`, `select_many`, `execute` and `execute_script`. Each call uses a
connection for that one statement only, which runs on its own (autocommit). The pool picks
the connection like this:

1. A connection with nothing in flight takes it.
2. Otherwise, below `max_size`, a new connection starts opening in the background; the
   statement doesn't wait for it.
3. Meanwhile, the statement is pipelined onto the connection with the least expected queued
   work, among those below `max_inflight_per_conn`.
4. If every connection is at that cap, it waits, first come first served.

!!! warning "Queries on the pool must not change session state"

    Their connection is shared with other callers and isn't reset. `SET`, `BEGIN`, temporary
    tables, advisory locks and `LISTEN` belong in `acquire()` or `transaction()`.

## Multi-statement work: `acquire()` and `transaction()`

`pool.acquire()` gives you a `fastpg.Connection` to yourself until the block ends;
`pool.transaction()` does the same and runs a [transaction](transactions.md) on it.

```python
async with await fastpg.create_pool(dsn) as pool:
    async with pool.acquire() as conn:
        await conn.execute_script("SET statement_timeout = '5s'")
        print(await conn.select_one("SHOW statement_timeout"))  # ('5s',)
    print(await pool.select_one("SHOW statement_timeout"))  # ('0',): reset on release
```

* Both wait, first come first served, for an idle connection, growing the pool if they can.
  If none is idle, the pool stops sending new queries to a busy connection and hands it over
  once it finishes. A released connection first takes the shared queries waiting for one,
  so callers that keep re-acquiring can't starve them.
* On release, one round trip rolls back an open transaction and resets the session
  (settings, role, advisory locks, cursors, `LISTEN`s, temporary tables), keeping prepared
  statements. If that fails, the connection is closed.
* The pool's [bulk writes](writes.md) also get a connection to themselves for the call.

## Head-of-line blocking

PostgreSQL runs one connection's queries in order, so a pipelined query waits for every
query queued ahead of it. Sharing a connection is fine while

> slowest query's time on the connection × queries in flight ≤ the latency you can accept

On Linux under one CPU ([Performance](../performance.md)), a pipelined point lookup
occupies a connection for about 7.7 µs, and a 10,000-row fetch for about 5.8 ms, which it
adds to every lookup queued behind it.

Besides preferring idle connections and capping the pipeline depth, the pool learns a moving
average of each SQL text's time on a connection (unseen SQL counts as 50 µs). A connection
scores the larger of its expected queued work and the age of its oldest statement in flight,
and the lowest score wins, so connections behind a known-slow query, or stuck behind an
unexpectedly slow one, are avoided.

A slow statement the pool hasn't seen yet can still delay lookups queued behind it. Run
known-slow queries on `acquire()` or a separate pool, set `statement_timeout`, and raise
`min_size` so that more connections are idle.

## Failures and gotchas

* A dead connection is dropped when picked or when a statement on it fails with a
  session-fatal error. Statements in flight on it fail; nothing is retried.
* A background task closes connections older than `max_lifetime` once idle, and idle ones
  above `min_size` after `max_idle`, and tops the pool up to `min_size`.
* `pool.close()` is graceful: new calls fail with `ConnectionClosedError`, queries already
  running complete, and connections taken with `acquire()` close when their block ends.
  It doesn't wait for the server to acknowledge the end of each session. Bound it with
  `asyncio.timeout(...)`: when it is cancelled, every connection still open, those taken
  with `acquire()` included, is closed at once (`Connection.terminate()`), and the queries
  on them fail with `ConnectionClosedError`.
* A cancelled query keeps running on the server, and its connection stays busy. Wrap calls
  in `asyncio.timeout(...)`, and put libpq's `connect_timeout` (seconds) in the DSN, or a
  connection being opened to a database that doesn't answer waits indefinitely.
* Each process and loop has its own pool: allow `processes × max_size` connections.
