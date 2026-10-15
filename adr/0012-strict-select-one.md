# 0012. Strict select_one

- Status: Accepted
- Date: 2026-10-10

## Context

Most drivers' `fetchone` silently returns the first row of a multi-row result, which hides
query bugs.

## Decision

- `select_one` raises `NoRowsError` (also a `LookupError`) for 0 rows and
  `TooManyRowsError` for more than 1.
- `select_one_or_none` returns `None` for 0 rows and raises for more than 1.

## Consequences

Correctness over convenience: an unexpected row count is always an error. The behaviour is
implemented in the `Shape::One` / `Shape::OneOrNone` handling in
`native/src/connection.rs`.
