# 0035. Two pydantic routes: json and direct

- Status: Accepted
- Date: 2026-10-10

## Context

Validating every row through pydantic is safe but slow; skipping validation is only correct
when it would be a no-op.

## Decision

- `"json"` (validated): Rust encodes rows as JSON, then
  `TypeAdapter(list[M]).validate_json` validates them.
- `"direct"`: a native `model_construct` equivalent, used only when walking
  `__pydantic_core_schema__` proves validation would be a no-op (see the
  [README](../README.md) for the exact rules).
- `"auto"` picks direct when allowed, otherwise json. `"msgpack"` is not offered for
  pydantic.

## Consequences

Models get the fast path when it is provably equivalent, and full validation otherwise.
