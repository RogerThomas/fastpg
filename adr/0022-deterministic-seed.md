# 0022. Deterministic seed data

- Status: Accepted
- Date: 2026-10-10

## Context

Benchmarks should verify that drivers return correct rows, not just fast ones.

## Decision

The seed has 100k `people` rows built from formulas, without `random()`.

## Consequences

Benchmarks verify returned rows against the same formulas, and runs are reproducible.
