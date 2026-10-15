# 0020. Structs are built through __init__

- Status: Accepted
- Date: 2026-10-10

## Context

Writing a msgspec Struct's slots directly would be faster but bypasses defaults,
`__post_init__` and msgspec's GC bookkeeping.

## Decision

msgspec Structs are built by calling `__init__` via vectorcall. Positional arguments are used
when the columns are the leading fields in order; otherwise keyword names reuse the Struct's
own interned field-name strings.

## Consequences

Struct semantics stay correct at a small cost over slot writes.
