# 0006. Query parameters

## Context

Values must be bound safely in three situations: short ad hoc queries, reusable constant
queries, and queries built where their values are. Table and column names cannot be bind
parameters.

## Decision

- Positional: PostgreSQL's `$1, $2, …`, with values passed as `*args`.
- Named: `:name` in a `str` query, with `params=` taking a msgspec Struct, dataclass,
  pydantic model (by attribute) or mapping (by key). A small scanner rewrites `:name` to
  `$n`, numbered by first appearance. It skips string literals, quoted identifiers,
  comments (including nested `/* */`) and dollar quotes; `::` is a cast and `\:` a literal
  colon. Rewrites are cached per query string and value getters per (query, params type),
  about 140 ns per call. Queries without `params=` are never scanned.
- t-strings (PEP 750, Python 3.14+): interpolations always become `$n` parameters, and
  nested templates are spliced in and renumbered. `fastpg.Identifier` interpolates a quoted
  name. Conversions, format specs and a literal `$<digit>` in template text are rejected.
  The generated SQL is cached per template and shares prepared statements with the
  equivalent `$n` query.

## Consequences

- Values never become SQL text; names do only through `Identifier`, which quotes them.
- A t-string cannot serve as a constant or be reused per row, so `execute_many` takes `$n`
  SQL, and `insert_many` / `copy_in` map fields to columns.
- The `$<digit>` rule rejects some valid text; such text can be passed as a value.
