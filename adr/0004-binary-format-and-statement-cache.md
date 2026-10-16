# 0004. Binary format and statement cache

## Context

The text format makes every value round-trip through a string, and preparing a statement
on every call costs a round trip. An unbounded statement cache leaks server memory.

## Decision

- Parameters and results always use PostgreSQL's binary format, encoded and decoded in
  Rust.
- Every query runs as a named prepared statement. Each connection keeps an LRU cache keyed
  by SQL text (`statement_cache_size`, default 256). An evicted statement is deallocated on
  the server once no in-flight query uses it. Each entry also caches its column decoders
  and its column-to-field bindings per `into=` type.
- Parameters are encoded for the types the server declared at prepare time. A mismatch,
  such as a naive datetime for `timestamptz`, raises `ParameterError` before anything is
  sent.
- A cached statement that has gone stale, after `DEALLOCATE ALL` / `DISCARD ALL` (SQLSTATE
  26000) or DDL that changes its result type (0A000), is re-prepared and retried once. Both
  errors occur at Bind, before execution, and the retry happens only if no rows were
  delivered. Inside a transaction block the error has aborted the transaction, so the
  original error is raised instead.
- A cached statement keeps the parameter types it was prepared with. A value that fails to
  encode for them (nothing has been sent) re-prepares the statement and retries once, in
  case a column changed type; a server error about them (42883, 42804) evicts the
  statement without a retry, as those can also be raised while executing.

## Consequences

- A repeated query costs one round trip.
- Schema changes and session resets do not break cached queries, and a retry cannot
  duplicate rows.
- There is no unnamed-statement mode, so fastpg does not work behind pgbouncer in
  transaction-pooling mode.
- Queries pipelined concurrently from different tasks are not ordered relative to each
  other: one that needs preparing can be overtaken by one already cached.
