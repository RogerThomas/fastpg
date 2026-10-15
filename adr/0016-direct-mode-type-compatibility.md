# 0016. Type compatibility in direct mode

- Status: Accepted
- Date: 2026-10-10

## Context

Direct construction bypasses msgspec, so fastpg defines which column types may feed which
annotations.

## Decision

- `int` from int2/int4/int8/oid.
- `float` from float4/float8, and from ints, widened as msgspec does.
- `str` from text-like types and enums, and from json/jsonb as raw text.
- `datetime` from timestamp (naive) and timestamptz (aware, UTC).
- `Decimal` from numeric only.
- JSON-shaped fields (list, dict, nested Struct, ...) from json/jsonb, decoded with a cached
  `msgspec.json.Decoder(<annotation>)`.

## Consequences

The msgpack and json strategies match direct mode: a json/jsonb column feeding a `str` field
is encoded as a string of its text, so every strategy gives that field the same value.
