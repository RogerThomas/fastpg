# 0009. No connection pool

- Status: Superseded by [0044](0044-pool-over-single-connection.md) and [0051](0051-pool-design.md)
- Date: 2026-10-10

## Context

A pool was not in the initial specification, and xitca supports pipelining concurrent
queries on one connection.

## Decision

Ship no pool. Concurrent queries on one connection are pipelined. The benchmarks'
concurrency scenario uses N connections round-robin, plus a `fastpg-pipelined` variant on a
single connection.

## Consequences

Superseded: measurements in [0044](0044-pool-over-single-connection.md) showed a single pipelined connection collapses under mixed
workloads, and [0051](0051-pool-design.md) adds a pool.
