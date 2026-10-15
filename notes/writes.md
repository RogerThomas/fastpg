# Bulk writes: `execute_many`, `insert_many`, `copy_in`

Code: `native/src/bulk.rs` (I/O tasks, encoders, row readers), `src/fastpg/_writes.py`
(chunking, column planning), `Connection.execute_many/insert_many/copy_in` in
`src/fastpg/_connection.py`. Tests: `tests/test_writes.py`. Benchmark: `bench/writes.py`.

## API

```python
await cur.execute_many(query, rows)  # rows: iterable of parameter sequences
await cur.insert_many(table, rows, columns=None)  # -> rows inserted
await cur.copy_in(table, rows, columns=None)  # binary COPY FROM STDIN -> rows copied
```

* `rows` for `insert_many` / `copy_in`: msgspec Structs (columns = *encoded* field names, so
  `rename=` works), pydantic models (columns = the alias `select_*` matches, i.e.
  `validation_alias` or `alias`), mappings (columns = the first row's keys; later rows must
  have exactly those keys) or sequences (columns = every table column, in order). All rows
  must be the same kind/type. `columns=` selects and orders the columns explicitly (for
  mappings, extra keys are then ignored).
* `table`: `"t"`, `"schema.t"` (split on dots) or a tuple `("we.ird", "t")`. Every part is
  quoted, so names match exactly (case-sensitively), and odd names are safe.
* Values: the same Python types as query parameters (`params.rs`), `None` → NULL. In bulk
  writes, json/jsonb columns also take anything msgspec can serialise (nested Structs,
  dataclasses, ...), and domain columns are encoded as their base type.
* Errors: `ParameterError` for values/rows that can't be encoded, raised *before* anything
  is committed; `DatabaseError` subclasses from the server. Both carry `row_index` (0-based
  input row; new class attribute on `FastPGError`, default `None`) and a PEP 678 note.
* Empty input returns 0 without contacting the server (the SQL isn't even checked).

## What xitca-postgres 0.4 supports (checked in the source)

Paths are relative to `~/.cargo/registry/src/*/xitca-postgres-0.4.0/src`.

* **Every request ends with a `Sync`.** All `Encode` impls (`driver/codec/encode.rs`:
  `encode_stmt_query`, `encode_statement_create`, `PortalCreate`, `PortalQuery`, ...) append
  `frontend::sync`, and `Encode` is sealed (`encode.rs`, `sealed::Sealed`), so there is no
  public way to send N × Bind/Execute followed by a single Sync.
* **Responses are routed per `ReadyForQuery`.** The driver splits incoming bytes at RFQ
  (`driver/codec.rs` `ResponseMessage::try_from_buf`) and hands each slice to the oldest
  registered receiver (`driver/generic.rs` `DriverRx::try_decode`). One request = one Sync
  = one RFQ.
* **Pipelining is free:** `execute()`/`query()` encode into the shared write buffer at call
  time (`execute/async_impl.rs`, `cli.query(self)`), so issuing many before awaiting any
  sends them in one write.
* **No per-statement results inside one Sync:** `RowAffected` keeps only the *last*
  `CommandComplete` (`driver/codec.rs` `poll_try_into_ready`); `RowStream` and
  `RowSimpleStream` skip `CommandComplete` (`query/stream.rs`).
* **Transactions and `copy_in` need `&mut Client`:** `Client::transaction(&mut self)`,
  `Client::copy_in(&mut self)`, `CopyIn<C: r#Copy + ClientBorrowMut>` (`client.rs`,
  `copy.rs`, `transaction.rs`). fastpg shares one `Arc<Client>` between pipelined tasks, so
  neither is usable.
* **Raw frontend messages are possible:** `dev::r#Copy::send_one_way(&self, ..)` is public
  and implemented for `Client` (`copy.rs`); it appends bytes to the write buffer *without*
  registering a response receiver (`driver/generic.rs` `DriverTx::send_one_way`). xitca's
  own `CopyIn` uses it for CopyData/CopyDone/CopyFail.
* A simple-query stream reports `CopyInResponse` as `error::UnexpectedMessage` (public,
  `error.rs`) and keeps reading (`query/stream.rs`); `DbError::where_()` exposes the
  `COPY t, line N` context.

## Decisions

### 1. A bulk write owns the connection (exclusive gate)

A connection is shared by concurrently pipelined tasks. A query from another task
landing between a bulk write's messages would join its transaction, or break a COPY stream
(the server rejects any non-COPY message mid-COPY, which then desynchronises xitca's
RFQ-based routing). So `Shared` has a `tokio::sync::RwLock<()>` gate: every ordinary
operation (`submit`, `execute_script`) holds it *shared* for its run, and a bulk write holds
it *exclusively*. Bulk writes wait for in-flight queries; queries issued meanwhile wait for
the bulk write (tokio's RwLock is fair), and never see a partial result
(`test_concurrent_queries_wait_for_the_bulk_write`). The uncontended shared acquire is a
single atomic operation. Operations post their completion *before* releasing the gate (see 5).

### 2. Atomicity: all-or-nothing, and an open transaction is left as it was

Setup pipelines a probe with the prepare (one round trip):
`SELECT set_config('fastpg.in_transaction','on', true)` then, as a separate simple query,
`SELECT current_setting('fastpg.in_transaction', true)`. A transaction-local setting only
survives past a `Sync` inside an explicit transaction block, so the second query answers
"is a transaction open?" with no side effects. (A failed transaction makes the probe fail
with 25P02, which is raised.)

* No transaction: `BEGIN` … rows … `COMMIT`. A row error aborts the transaction, so the
  pipelined `COMMIT` rolls back. Deferred constraints fail at `COMMIT`, reported with
  `row_index=None`.
* Inside the caller's transaction: `SAVEPOINT fastpg_bulk` … `RELEASE`. On any error or
  abort: `ROLLBACK TO SAVEPOINT` + `RELEASE`. The error is raised, but the caller's
  transaction is **still usable** and unchanged. This differs on purpose from a failing
  `execute()`, which leaves the transaction aborted. The reason is cancellation: without
  the savepoint, cancelling a bulk write inside a transaction would leave a partial set of
  rows that the caller might commit.
* COPY is one statement (atomic by itself); inside a transaction it gets the same savepoint
  treatment.
* Cancellation (the awaiting task is cancelled) before the last chunk was handed over →
  abort → rollback as above; the connection stays usable
  (`test_cancellation_rolls_back_and_keeps_connection_usable`, 6 variants). Once `finish()`
  was sent the write completes server-side, like any cancelled query.

### 3. `execute_many` = one statement per row, one Sync per chunk; `insert_many` = 64 rows per statement

*Updated by `notes/perf-writes.md`:* each chunk's statements now share one `Sync`. All but
the last statement are encoded by xitca, stripped of their trailing `Sync` and sent with
`send_one_way`; the last is an ordinary request, whose receiver gets the whole cycle. Since
xitca only reports a cycle's last `CommandComplete`, the per-statement results come from a
tap in `EofWatch` (`net.rs`): it follows the message framing of everything read (always, so
it is at a message boundary whenever a bulk write starts), and while a bulk write collects,
records per cycle the completed statements, the summed row counts and whether an
`ErrorResponse` came (after how many statements). Every request of the bulk task consumes one
cycle, in order. The only other requests that can be in flight during the exclusive gate are
`Close`s of dropped cached statements (fire and forget); their cycles start with
`CloseComplete` and are skipped. So is a cycle still under way when collecting starts:
xitca resolves a prepare (and its type lookups) before reading its `ReadyForQuery`, so
whether a cycle is recorded is decided when its first message arrives. If the tap ever disagrees with xitca (a successful cycle
whose statement count doesn't match), the write fails and rolls back rather than return a
wrong count. Results: per-row counts and the exact failing `row_index` as before, at
~266k rows/s (was ~83k) and ~560 ns/row of loop CPU (was ~9 µs) on macOS.

What follows describes the earlier design.

Because of the Sync-per-request constraint above, `execute_many` sent Bind/Execute/Sync per
row, all pipelined (no round trip per row). That gives exact per-row affected counts (summed)
and the exact failing `row_index`. The price is that the server flushes a `ReadyForQuery`
per row, and (on the threaded path these runs used) fastpg's I/O thread woke per row. In the
smoke runs that's ~6 µs/row of I/O-thread CPU and ~85–120k rows/s, while asyncpg's executemany (one Sync per batch) reaches ~250k. The
loop-thread CPU is 3.5x lower than asyncpg's (~620 vs ~2,100 ns/row).

`insert_many` generates its SQL, so it amortises the Sync: one statement per 64 rows
(`INSERT … VALUES ($1..$10), ($11..$20), …`), plus a one-row statement for the < 64 rows
left at the end. 64 is a power of two, so it divides the 1024-row chunk, and it is halved
until the parameters fit in 65,535. Measured with asyncpg as the client, then with fastpg:
1 → 16 → 64 → 512 rows/statement gave 251k → 448k (asyncpg) and 363k(16) → 441k(64) →
487k(512) (fastpg). Gains flatten after 64, and larger groups make errors vaguer. The cost:
a database error inside a multi-row statement can't be pinned to a row (the server reports
per statement). `row_index` is `None` and the note says "raised by the multi-row INSERT of
rows 2496..2559; use execute_many() for the exact row". The server's DETAIL usually names
the offending key. Encoding errors (`ParameterError`) still carry the exact `row_index`.

Rejected alternatives (the first two are now combined, see the update above):
* Single-Sync pipelines via `send_one_way` + one terminating request. Responses would route
  correctly under the exclusive gate, but no xitca response reader exposes per-statement
  `CommandComplete`s, so counts and failing rows would be lost.
* Parsing the server byte stream in fastpg's socket wrapper to count `CommandComplete`s:
  too fragile. (It isn't, once the framing is followed from the first byte and foreign
  cycles are told apart: see the update.)
* A savepoint per group to locate failures: subtransaction overflow (> 64) hurts the whole
  server.

The proper fix is an upstream xitca API for "N binds, one Sync, per-statement results".

### 4. Fresh named statements per call (not the statement cache)

The bulk statement(s) are prepared in the setup round trip (pipelined with the probe) and
closed at the end. This has two benefits. A stale cached statement (DDL, `DEALLOCATE ALL`) can
never fail half-way through a pipeline, where re-preparing would require replaying rows.
And big generated INSERTs don't evict the user's cached statements. The cost is one
Parse/Describe per call, with no extra round trip.

### 5. COPY protocol details

* The target's column types come from describing `SELECT <cols> FROM <table> LIMIT 0` (or
  `*`), prepared in the setup round trip together with the probe. The COPY then names the
  columns explicitly, quoted from the description.
* COPY is sent as a **simple Query** (`COPY t (…) FROM STDIN (FORMAT binary)`), and the binary
  header + data go out right behind it via `send_one_way`, without waiting for
  `CopyInResponse`. This is safe only for the simple protocol. If the COPY fails to start
  (e.g. a view, `test_copy_that_fails_to_start_keeps_connection_in_sync`), the server sends
  ErrorResponse + one RFQ and ignores the CopyData/CopyDone that follow. With the extended
  protocol (xitca's `CopyIn`) there would be a second Sync and thus a stray RFQ, which is why
  `CopyIn` waits a round trip first. Finish = trailer + `CopyDone` (no Sync). Abort =
  `CopyFail` (server answers 57014 + RFQ).
* Errors during the COPY arrive while data is still being sent (the stream is polled
  concurrently), which stops the sending early. The failing row comes from the error
  context `COPY t, line N` (N = tuple number in binary format) → `row_index = N - 1`.
* The COPY is started by the **first chunk**, not at setup. That chunk is fed only after
  Python saw `ready`, i.e. after the loop thread processed every completion posted before it
  (ordinary ops post before releasing the gate). Dropping those completions can deallocate an
  evicted cached statement (a `Close` message), which must never land inside the COPY stream.
* **Backpressure:** xitca's write buffer is unbounded and invisible. fastpg's socket
  wrapper (`net.rs` `EofWatch`, now with an `IoWatch`) counts written bytes. A chunk is
  acknowledged only once at most 4 MiB of the COPY data handed to xitca is still unwritten,
  so a slow server can't make memory grow with input size. (When xitca's own connect is
  used as a fallback, nothing is counted and chunks are acknowledged immediately.)

### 6. Encoding and reading rows (loop thread, chunked)

* `_writes.py` feeds chunks (1024 rows for execute/insert, 4096 for COPY), with at most 2
  queued for the native I/O task. Each chunk is read and encoded in Rust while the GIL is held,
  ~0.6 ms per 1024 rows, and the event loop runs between chunks. Rows can come from any
  iterable, including generators (200k-row streaming test).
* Values are encoded straight to PostgreSQL binary for the server-declared types. Fast paths
  cover exact `str`/`int`/`float`/`bool`/`bytes` for their natural types. Everything else
  goes through `params::convert` + the existing `ToSql` encoders, so semantics and error
  messages match query parameters (`convert` is `from_py` minus the parameter-position
  wrapper). `execute_many` binds the pre-encoded bytes via a raw `ToSql`.
* Row readers (`RowReader`): tuple/list fast paths, then any non-text sequence. Dict fast
  path, then any mapping. msgspec Structs of the exact type are read **directly from their
  object slots** (offsets from the member descriptors, as for the UUID fast path), with
  `getattr` for subclasses. Pydantic models are read from `__dict__`.

### 7. Smaller choices

* `copy_in` with sequence rows and no `columns=` copies into every column of `SELECT *`.
  For tables with generated columns, pass `columns=` (the server rejects them otherwise).
* The probe leaves a session-level placeholder setting `fastpg.in_transaction = ''` visible
  in `SHOW ALL`.
* Row errors are numbered from 0 in the caller's input.

## Smoke benchmark (host only: macOS + Docker Desktop, shared machine, noisy)

```
FASTPG_DSN=postgresql://fastpg:fastpg@127.0.0.1:55432/fastpg_writes uv run yeet ./bench/writes.py --repeat 3
```

People-shaped rows (10 columns, `LIKE people INCLUDING ALL`, PK index), truncated before each
run, best of 3. "loop" = event-loop thread CPU (`time.thread_time`); "process" includes
fastpg's I/O thread, which these runs still had (they predate loop-native I/O, [ADR 0002](../adr/0002-protocol-on-the-event-loop.md)).

| method | 1k rows/s | 10k rows/s | 100k rows/s | loop ns/row | process CPU, 100k |
|---|---:|---:|---:|---:|---:|
| fastpg `execute_many` (tuples) | 47–87k | 75–118k | 82–118k | ~630 | 570–850 ms |
| fastpg `insert_many` (Structs) | 165k | 380k | **440k** | ~590 | 100 ms |
| fastpg `copy_in` (Structs) | 219k | 597k | **1.11M** | ~570 | 65 ms |
| fastpg `copy_in` (tuples) | 197k | 673k | **1.10M** | ~560 | 64 ms |
| asyncpg `executemany` | 135k | 255k | 269k | ~2,050 | 202 ms |
| asyncpg `copy_records_to_table` | 226k | 506k | 724k | ~1,320 | 132 ms |
| psycopg 3 async `executemany` | 27k | 28k | 28k | ~35,000 | 3,527 ms |
| psycopg 3 async `copy` (binary) | 213k | 774k | 1.04M | ~900 | 89 ms |

Ranges are from several runs (other workloads share the machine). Takeaways:

* `copy_in` is the fastest COPY measured, with the least client CPU: 1.5x asyncpg's
  throughput at 100k rows, 2.3x less loop CPU than asyncpg, 1.6x less than psycopg.
* `insert_many` beats asyncpg's `executemany` by 1.6x (via multi-row statements).
* `execute_many` loses to asyncpg on wall time here (xitca's per-request Sync, see 3) while
  using 3x less loop CPU. Linux numbers (lower per-packet cost) need a separate run.

## Limitations / open questions

* `execute_many` reads per-statement results from the socket tap (see 3); an upstream xitca
  API for "N binds, one Sync, per-statement results" would make the tap unnecessary.
* `insert_many` reports a 64-row range rather than a row for server errors.
* Bulk writes block other queries on the same connection for their duration (by design).
* Arrays/intervals etc. are as unsupported as for query parameters. Nested *pydantic*
  models in json columns aren't encoded (msgspec can't serialise `BaseModel`; dump them
  first).
* The Python style guide is applied to all new/changed Python. The Rust side keeps Rust
  naming (`PlanKind::Copy`, `Msg::Copy`, like the existing `Codec::Json`/`Uuid`).
