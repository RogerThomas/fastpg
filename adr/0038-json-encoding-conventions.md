# 0038. JSON encoding conventions

- Status: Accepted
- Date: 2026-10-10

## Context

Rows encoded as JSON for the json strategy must decode correctly in both msgspec and
pydantic.

## Decision

- numerics and UUIDs are strings;
- timestamptz is ISO-8601 with `Z`;
- bytea is base64 (msgspec's convention);
- non-finite floats are `NaN`/`Infinity` tokens;
- json/jsonb is embedded verbatim, except for a column feeding a `str` field, which gets a
  string of its text ([0016](0016-direct-mode-type-compatibility.md)).

## Consequences

pydantic needs `model_config = ConfigDict(val_json_bytes="base64")` for bytes fields via
`"json"`. Non-finite floats are accepted by pydantic and rejected by msgspec.
