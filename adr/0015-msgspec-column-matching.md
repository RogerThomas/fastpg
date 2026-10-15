# 0015. Column matching follows msgspec decoding

- Status: Accepted
- Date: 2026-10-10

## Context

Rows must map onto Struct fields the way msgspec users expect.

## Decision

- Columns are matched by encoded field name, so `rename=` works.
- Extra columns are ignored unless `forbid_unknown_fields=True`.
- Missing fields with defaults use the default; missing required fields raise
  `MissingColumnError`.
- This binding, plus the column-type vs annotation check, runs once per (statement, Struct)
  and is cached.

## Consequences

Behaviour matches `msgspec.convert`. Because the check also runs for empty results,
mismatches surface early rather than on the first non-empty result.
