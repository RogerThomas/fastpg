# 0044. A pool rather than one pipelined connection

- Status: Accepted
- Date: 2026-10-10

## Context

"One connection is all you need" was tested with `bench/mixed.py` on Linux, with 1% of
requests being 20 ms queries. A single pipelined connection collapsed (fast-query p99 776 ms
at 5k/s), because PostgreSQL runs one connection's queries in order. Unbounded pipelining
also leaks slow-query delay into fast queries' tails; asyncpg's exclusive per-query pool had
the steadiest tail.

## Decision

Add a pool that:

- uses idle connections first;
- grows up to `max_size`;
- pipelines only up to `max_inflight_per_conn` (default tuned from measurements);
- otherwise queues FIFO;
- offers `acquire()` and `transaction()` for exclusive use.

## Consequences

Supersedes [0009](0009-no-connection-pool.md). Details of the design are in [0051](0051-pool-design.md).
