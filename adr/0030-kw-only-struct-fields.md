# 0030. Respect kw_only Struct fields

- Status: Accepted
- Date: 2026-10-10

## Context

Direct construction passes leading fields positionally ([0020](0020-structs-built-via-init.md)), but msgspec `kw_only`
fields must not be. msgspec exposes kw-only-ness only through `inspect.signature`.

## Decision

`kw_only` fields are never passed positionally. The signature is read once per Struct type.

## Consequences

Correct for every Struct, with the introspection cost paid once.
