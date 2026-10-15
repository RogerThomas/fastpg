# 0039. Rename select_all to select_many

- Status: Accepted
- Date: 2026-10-10

## Context

`select_many` reads better next to `select_one` and `select_one_or_none`.

## Decision

`select_all` is renamed to `select_many` across the API, tests, benchmarks, docs and the
`TooManyRowsError` hint.

## Consequences

No deprecated alias is kept, since nothing had been released.
