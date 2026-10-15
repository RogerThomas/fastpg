# 0036. Directly built models follow model_construct semantics

- Status: Accepted
- Date: 2026-10-10

## Context

The direct route ([0035](0035-pydantic-json-and-direct-routes.md)) must produce models indistinguishable from pydantic's own.

## Decision

- `model_fields_set` contains only the fields that came from columns.
- Defaults fill the rest, but only immutable ones (`None`/bool/int/float/str/bytes); mutable
  defaults force the json route, because pydantic copies them per instance.
- `__pydantic_extra__` and `__pydantic_private__` are `None`.
- timestamptz values carry `datetime.UTC`.

## Consequences

pydantic's own JSON parsing gives `TzInfo(UTC)` instead of `datetime.UTC`; the values compare
equal.
