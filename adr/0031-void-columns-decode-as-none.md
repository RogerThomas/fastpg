# 0031. void columns decode as None

- Status: Accepted
- Date: 2026-10-10

## Context

Queries such as `SELECT pg_sleep(1)` return a `void` column.

## Decision

`void` columns decode as `None`.

## Consequences

Such queries work with every result method instead of raising an unsupported-type error.
