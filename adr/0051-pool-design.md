# 0051. Pool design

- Status: Accepted
- Date: 2026-10-10

## Context

[0044](0044-pool-over-single-connection.md) establishes the need for a pool. Details are in [notes/pool.md](../notes/pool.md).

## Decision

- **API:** shared pipelined queries with no acquire; `acquire()` and `transaction()` for
  exclusive use.
- **Routing:** idle connection, else grow, else the least expected work under the
  per-connection cap, else FIFO wait.
- **Defaults:** `max_inflight_per_conn=8`, `max_size=16`. On Linux at 20k req/s with 1% slow
  queries, 8 connections saturated (fast p99 22 ms) while 16 held 112 µs. Connections only
  open when all are busy, so the higher ceiling costs nothing at low load.
- COMMIT of an aborted transaction raises (SQLSTATE 25P02) instead of PostgreSQL's silent
  rollback.

## Consequences

Concurrent use of a plain `Connection` during `conn.transaction()` is documented, not
guarded. There is no server-side cancel and no connect timeout or backoff; both are listed
as limitations.
