# Limitations

fastpg is an early prototype, with these known gaps.

* **Unix only** (macOS and Linux): the socket is registered with `loop.add_reader` /
  `add_writer`, which Windows' default proactor loop doesn't offer.
* **One event loop per connection.** A connection must only be used from the loop that
  opened it. This isn't checked: using it from another loop or thread, or in a child process
  after `fork()`, misbehaves rather than raising.
* **Free-threaded CPython** (3.14t) isn't targeted: the extension declares
  `gil_used = true`, as it hasn't been audited for running without the GIL, so importing
  fastpg re-enables the GIL.
* **Types not decoded yet:** arrays, `interval`, `timetz`, ranges, composite types, and
  infinite dates and timestamps. No array parameters either (see
  [Queries](guide/queries.md#gotchas)).
* **No pgbouncer** in transaction-pooling mode: statements are always prepared and named.
* **No LISTEN/NOTIFY**, and no `COPY TO`.
* **No server-side cancel**: a cancelled query keeps its connection busy until it finishes.
  Connecting is bounded only by libpq's `connect_timeout` DSN key (off by default), and a
  pool retries a down database without backoff.
* **Concurrent queries on one connection aren't ordered** with respect to each other: a
  statement that needs preparing can be overtaken by one that's prepared.
* **TLS gaps**: see [TLS](guide/tls.md#not-supported).
