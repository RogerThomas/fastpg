# 0048. Bulk writes

- Status: Accepted
- Date: 2026-10-10

## Context

Inserting many rows one query at a time is slow. Details are in
[notes/writes.md](../notes/writes.md).

## Decision

Provide `execute_many`, `insert_many` (64 rows per statement) and `copy_in` (binary COPY).
Each runs in its own `BEGIN...COMMIT`, or a `SAVEPOINT` inside a caller's transaction, so a
failed bulk write leaves the caller's transaction usable (unlike `execute()`, and safer for
cancellation). No `rows_per_statement=` option for now.

## Consequences

- `insert_many` errors report a row range rather than an exact row.
- `execute_many` is slow (82-118k rows/s) because xitca 0.4 forces one Sync per row. Fixing
  it needs an upstream API ("many binds, one Sync, per-statement results"); the plan is an
  upstream issue, not vendoring.
