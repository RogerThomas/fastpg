# 0034. Optional pydantic support

- Status: Accepted
- Date: 2026-10-10

## Context

pydantic users want `into=` to build `BaseModel`s, but msgspec users should not pay for
pydantic.

## Decision

pydantic v2 `BaseModel` is supported by `into=` as an optional extra. fastpg checks
`sys.modules` and only touches pydantic if the application already imported it. The `into=`
hint is an unbounded `type[T] -> T`, so typing works for both libraries without importing
pydantic.

## Consequences

pydantic is never a hard dependency, and importing fastpg never imports it.
