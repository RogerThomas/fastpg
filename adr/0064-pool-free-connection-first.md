# 0064. Pool routing: free connection first

- Status: Accepted
- Date: 2026-10-11

## Context

Joining a lightly loaded busy connection even when one is free was tried (details in
[notes/perf-pool.md](../notes/perf-pool.md), "Pipelining earlier under load"). It saved ~10%
CPU per lookup and gave +16% ops/s at 16 tasks, but doubled the slow-query p99 in
`bench/mixed.py` (~21 ms to 29.7-38.7 ms): slow statements queued behind each other. A safer
variant kept the tails but gained ~4%.

## Decision

Keep the first rule of [0044](0044-pool-over-single-connection.md): a statement takes an idle connection when there is one, and
joins a busy one only when none is idle.

## Consequences

For APIs, steady tail latency matters more than peak throughput. Small pools are faster for
uniform lookups (1 CPU, 16 tasks: pools of 1/2/4/16 did 91k/101k/85k/63k ops/s), the same
trade-off. Revisit only with new evidence; the untested next idea is joining only below
`min(2 x own cost, B)`, with B a few point lookups tuned on Linux.
