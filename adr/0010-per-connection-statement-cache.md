# 0010. Per-connection LRU statement cache

- Status: Accepted
- Date: 2026-10-10

## Context

Every query runs as a prepared statement. Re-preparing on each call costs a round trip, and
an unbounded cache leaks server memory.

## Decision

Each connection keeps an LRU cache of 256 prepared statements, keyed by SQL text and
configurable with `connect(..., statement_cache_size=)`. An evicted statement is deallocated
on the server once no in-flight query uses it.

## Consequences

There is no "unprepared" mode, e.g. for pgbouncer transaction pooling: xitca's
single-round-trip unnamed path needs explicit parameter types up front.
