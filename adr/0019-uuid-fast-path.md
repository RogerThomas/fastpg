# 0019. UUID construction fast path

- Status: Accepted
- Date: 2026-10-10

## Context

`uuid.UUID.__init__` is pure Python and costs about 280 ns per value.

## Decision

fastpg allocates the `UUID` instance and fills its two `__slots__` (`int`,
`is_safe=SafeUUID.unknown`) directly, as the uuid module does internally. The slot layout is
checked at startup, with a fallback to `UUID(int=...)`.

## Consequences

UUID decoding is much cheaper. Tests cover equality, hashing, `version`, `is_safe` and
immutability to guard against layout changes.
