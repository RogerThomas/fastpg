# Architecture

fastpg starts no threads. The asyncio loop thread does everything: socket I/O,
xitca-postgres's protocol work, and decoding rows into Python objects, so a query costs no
thread hand-off.

```
 asyncio loop thread
 ───────────────────
 connect(dsn)
   ├─ socket connected in Python (host names via loop.getaddrinfo)
   └─ loop.add_reader / add_writer(socket) → Rust readiness callbacks
 conn.select_one(query, 42, into=Person)
   ├─ params → Rust values, statement cache lookup
   └─ spawn a query task on the connection's Executor, poll it at once
        xitca-postgres: prepare (on a cache miss), bind, execute; binary, pipelined
 on_readable()
   ├─ poll the woken tasks; rows are copied into a RowBuffer
   └─ for each completed query: bind columns to fields (cached),
        decode cells, call Person(...), future.set_result(...)
```

## The pieces

* **`Executor` and `LoopSocket`** (`native/src/event_loop.rs`): one executor per connection
  polls xitca-postgres's futures on the loop thread, with the GIL held, whenever the loop
  reports readiness or Python submits work. `LoopSocket` implements xitca's I/O trait over
  the socket, fed by the `add_reader` / `add_writer` callbacks, and reads it through a
  16 KiB read-ahead buffer (xitca reads into buffers of about 4 KiB). There is no tokio
  runtime; TLS (rustls) runs between the socket and xitca. A waker fired from another
  thread (e.g. a `__del__` run by the garbage collector) writes one byte to a socketpair the
  loop watches.
* **`EofWatch`** (`native/src/net.rs`): wraps the stream so that a zero-byte read or a fatal
  I/O error closes the connection, and every pending and later operation fails at once with
  `ConnectionClosedError` instead of hanging. It also counts the bytes written, which gives
  `copy_in` its back-pressure, and records per-statement results for `execute_many` and
  `insert_many`. It follows the messages written too: once `close()`'s Terminate is
  flushed, it reports EOF itself, so the close doesn't wait for the server.
* **Server-side cancel** (`native/src/cancel.rs`): a query whose caller gives up while it is
  alone on its connection gets a CancelRequest, on a socket Python connects to the same
  address and the connection's executor drives (TLS when the connection uses it). Until the
  server has closed that socket, the connection's other operations wait before sending, so
  the cancel can't land on the next query.
* **`RawCell` and `RowBuffer`** (`native/src/rows.rs`): `RawCell` takes any PostgreSQL value
  as its untouched binary bytes. Each result's cells are copied into one `RowBuffer` arena,
  one `memcpy` per cell. `select_many` hands rows over every 512 rows, so decoding overlaps
  the transfer. A decoded result's buffer goes back to its connection, which keeps up to
  256 KiB of them for its next results.
* **Struct construction** (`native/src/structs.rs`, `src/fastpg/_spec.py`): a decoder per
  column is cached with the prepared statement, and the column-to-field binding once per
  (statement, type). Each row is decoded and passed to the Struct's own `__init__` through
  vectorcall, so defaults and `__post_init__` behave as usual. Pydantic models are built the
  way `model_construct` builds them.
* **The pool's `Router`** (`native/src/router.rs`): per connection, the pool queries in
  flight and their expected costs, and the learned cost of each SQL text. The idle stack,
  waiters and growth stay in Python (`src/fastpg/_pool.py`). Every routing decision runs on
  the loop thread between awaits, so neither half needs locks.

Bulk writes encode rows in Rust a chunk at a time on the loop thread, and hold their
connection exclusively while they run (see [Writes, bulk and COPY](guide/writes.md)).

The decisions behind this design, and their trade-offs, are recorded as
[architecture decision records](https://github.com/RogerThomas/fastpg/blob/main/adr/README.md).
