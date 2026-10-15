# 0017. Unsupported types fail with clear errors

- Status: Accepted
- Date: 2026-10-10

## Context

Some PostgreSQL types have no direct Python equivalent or were out of the initial scope.

## Decision

The following are unsupported, each with a clear error: arrays, interval, timetz, ranges,
composite types, ±infinity dates and timestamps (Python cannot represent them), and years
outside 1..9999.

## Consequences

Queries touching these types fail loudly rather than returning wrong values. Arrays are the
most likely next addition.
