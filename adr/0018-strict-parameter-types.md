# 0018. Strict parameter type checks

- Status: Accepted
- Date: 2026-10-10

## Context

Parameter types are known from the server's statement description, so mismatches can be
caught before sending. asyncpg silently assumes UTC or local time for mismatched datetimes.

## Decision

Parameters are checked strictly against server-declared types:

- a naive datetime for a `timestamptz` parameter is an error, as is an aware datetime for
  `timestamp`;
- `str` is accepted for text-like types, uuid, numeric and json;
- `dict` and `list` are encoded as JSON via msgspec;
- an `Enum` is sent as its `.value`.

## Consequences

Timezone mistakes surface as errors instead of shifted timestamps.
