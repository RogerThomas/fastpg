# 0007. Bulk writes

## Context

Inserting many rows one query at a time is bound by round trips. xitca-postgres 0.4 hands a
request only the last `CommandComplete` of its response cycle, so pipelining many
statements behind one `Sync` loses their individual results.

## Decision

- `execute_many(sql, rows)` runs one prepared statement per parameter row, 1,024 rows per
  `Sync`. A tap on the response framing in `EofWatch` recovers each statement's result and
  the failing row.
- `insert_many(table, rows)` sends multi-row `INSERT … VALUES`, 64 rows per statement, from
  Structs, models, mappings or sequences, with quoted identifiers.
- `copy_in(table, rows)` streams binary `COPY FROM STDIN`, encoded in Rust in 4,096-row
  chunks, with back-pressure from socket progress.
- Each write runs in `BEGIN … COMMIT`, or in a `SAVEPOINT` when a transaction is open,
  detected with a side-effect-free `set_config(..., is_local)` probe. A failed or cancelled
  write leaves the caller's transaction intact. Errors carry `row_index`.
- A bulk write holds its connection's read/write gate exclusively; other queries hold it
  shared.

## Consequences

- 100,000 ten-column rows under one CPU: `copy_in` 1.25M rows/s, `insert_many` 540k,
  `execute_many` 292k (asyncpg `executemany` 287k).
- Other queries on that connection wait during a bulk write; the pool avoids this with an
  exclusive checkout.
- `insert_many` errors identify a 64-row range rather than an exact row.
- The probe's placeholder setting appears in `SHOW ALL`.
