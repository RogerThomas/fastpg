# Limitations

fastpg is alpha software. This is what it can't do yet.

* **No Windows.** fastpg runs on Linux and macOS.
* **Some types can't be read yet:** `interval`, `timetz`, ranges, composite types,
  multi-dimensional arrays, and `infinity` dates and timestamps.
  See [Types](guide/types.md) for the types fastpg supports, and the workaround.
* **pgbouncer in transaction mode needs pgbouncer 1.21 or later**, with
  `max_prepared_statements` above 0 (the default is 200 from pgbouncer 1.24).
  fastpg always uses named prepared statements, so older pgbouncer, or
  `max_prepared_statements = 0`, doesn't work.
* **No `COPY ... TO`.**
* **One event loop per connection.** Use a connection or pool only from the event loop
  that opened it. A pool isn't thread-safe.
  After `fork()`, open new connections in the child process.
* **Cancelling a query** stops it on the server only when it is the only query running on
  its connection.
* **Queries sent at the same time on one connection may not run in the order you sent them.**
* **Free-threaded Python** (3.14t, 3.15t) works, but importing fastpg turns the GIL back on.
* **Some TLS options aren't supported.** See [TLS](guide/tls.md).
