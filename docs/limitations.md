# Limitations

fastpg is alpha software. This is what it can't do yet.

* **No Windows.** fastpg runs on Linux and macOS.
* **Some types can't be read yet:** arrays, `interval`, `timetz`, ranges, composite types,
  and `infinity` dates and timestamps.
  You can't pass arrays as parameters either.
  See [Queries](guide/queries.md#not-supported-yet) for workarounds.
* **No pgbouncer in transaction pooling mode**, because fastpg always uses named prepared
  statements.
* **No `LISTEN`/`NOTIFY`**, and no `COPY ... TO`.
* **One event loop per connection.** Use a connection or pool only from the event loop
  that opened it. A pool isn't thread-safe.
  After `fork()`, open new connections in the child process.
* **Cancelling a query** stops it on the server only when it is the only query running on
  its connection.
* **Queries sent at the same time on one connection may not run in the order you sent them.**
* **Free-threaded Python** (3.14t, 3.15t) works, but importing fastpg turns the GIL back on.
* **Some TLS options aren't supported.** See [TLS](guide/tls.md).
