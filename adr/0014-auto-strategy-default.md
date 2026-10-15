# 0014. strategy="auto" by default

- Status: Accepted
- Date: 2026-10-10

## Context

Building Structs directly is fastest, but it cannot apply msgspec validation such as `Meta`
constraints.

## Decision

`strategy="auto"` is the default. It uses direct construction when the Struct allows it.
When a field needs msgspec validation (`Meta` constraints, enums, literals, non-`None`
unions, `tz=` constraints), it uses the msgpack path so msgspec validates.
`strategy="direct"` raises `UnsupportedTypeError` for such Structs.

## Consequences

Constraints are never silently skipped. Callers who need the direct path's speed get an
explicit error instead of unvalidated data.
