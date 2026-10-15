# 0013. Rows are tuples without into=

- Status: Accepted
- Date: 2026-10-10

## Context

Queries need a default row type when the caller does not ask for a Struct or model.

## Decision

Without `into=`, rows are returned as tuples.

## Consequences

The default is the cheapest row type to build. Named access requires `into=`.
