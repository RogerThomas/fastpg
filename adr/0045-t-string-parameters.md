# 0045. Parameters from t-strings

- Status: Partly superseded by [0065](0065-named-parameters.md)
- Date: 2026-10-10

## Context

Binding values by name usually means a hand-written SQL lexer for `:name` placeholders.

## Decision

Use Python 3.14 t-strings, e.g. `t"... WHERE id = {sel.id}"`. The language separates SQL text
from values, so no lexer is needed and values are always bound parameters. Bulk writes are
table-oriented instead (`insert_many` / `copy_in` map Struct fields to columns by name),
because a t-string cannot be reused per row.

## Consequences

[0065](0065-named-parameters.md) later adds `:name` parameters via `params=` for reusable constant queries; t-strings
remain for queries built where their values are.
