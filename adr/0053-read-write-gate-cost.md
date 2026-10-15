# 0053. The read/write gate has no measurable cost

- Status: Accepted
- Date: 2026-10-10

## Context

The gate from [0049](0049-read-write-gate.md) adds work to every query.

## Decision

Keep the gate. Measured on Linux with the same scenarios before and after: point lookups
18,288 to 18,648 ops/s; 16-task pipelined 135.6k to 132.8k ops/s.

## Consequences

Both differences are within run-to-run noise.
