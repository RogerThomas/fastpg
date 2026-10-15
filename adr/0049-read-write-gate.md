# 0049. Per-connection read/write gate

- Status: Accepted
- Date: 2026-10-10

## Context

A COPY or a multi-statement bulk transaction must not interleave with pipelined queries on
the same connection.

## Decision

Each connection has a read/write gate. Normal queries hold it shared; bulk writes hold it
exclusively.

## Consequences

The cost is an uncontended atomic per query; the measurement is in [0053](0053-read-write-gate-cost.md) and
[perf-notes.md](../perf-notes.md).
