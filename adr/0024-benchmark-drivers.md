# 0024. Benchmark drivers and settings

- Status: Accepted
- Date: 2026-10-10

## Context

Comparisons are only meaningful against the drivers people actually use, configured as
they ship.

## Decision

Benchmark against asyncpg, psycopg 3 and psqlpy (async), and psycopg 3, psycopg2 and pg8000
(sync), all with default settings: psycopg stays at `autocommit=False` and prepares a
statement after 5 executions. A "-struct" variant of each builds `Person` the driver's
idiomatic way.

## Consequences

Results reflect out-of-the-box behaviour rather than per-driver tuning.
