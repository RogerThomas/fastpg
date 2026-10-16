# 0005. Connection pool

## Context

PostgreSQL runs one connection's queries in order. With 1% of requests being 20 ms queries,
one shared pipelined connection reached a fast-query p99 of 776 ms at 5k req/s. A pool with
one query per connection is robust but forgoes pipelining, which roughly halves CPU per
statement.

## Decision

- Shared queries (`pool.select_*`, `execute`) are routed per statement, without an
  acquire: to a connection with nothing in flight; otherwise the pool opens one in the
  background (up to `max_size`, default 16) and meanwhile pipelines onto the busy
  connection with the lowest stall score, up to `max_inflight_per_conn` (default 8);
  otherwise the statement waits in a FIFO queue and is sent by the code freeing a slot.
- The stall score is the larger of expected queued work, a moving average of each SQL
  text's service time, and the age of the oldest statement in flight. Rust (`Router`) keeps
  this bookkeeping.
- `acquire()` and `transaction()` check a connection out exclusively; on release, session
  state is reset in one round trip, keeping prepared statements. Bulk writes also check
  out.

## Consequences

- At 20k req/s with 1% slow queries, fast-query p99 was 112 µs, against 1.2 ms for an
  asyncpg pool of 16.
- Joining a busy connection while one is idle gave 16% more ops/s at 16 tasks, but doubled
  slow-query p99 (21 ms to 30-39 ms), so pipelining waits until every connection is busy.
- A cancelled statement is cancelled on the server only when it is alone on its connection; a
  pipelined one runs to its end. There is no connect backoff (the DSN's `connect_timeout`
  bounds a connect).
