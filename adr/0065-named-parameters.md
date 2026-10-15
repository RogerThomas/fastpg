# 0065. Named :name parameters via params=

- Status: Accepted
- Date: 2026-10-12

## Context

A t-string ([0045](0045-t-string-parameters.md)) is rebuilt at every call site, so it cannot be a reusable constant query.
A `:name` query can, which justifies a small SQL scanner.

## Decision

Add `params=`, taking a Struct (e.g. `params=PersonSelect(id=42, min_age=30)`) or a dict.
The scanner rewrites `:name` to `$n` outside quotes, comments and dollar quotes (`::` is a
cast, `\:` a literal colon). Rewrites are cached per query string, and value getters per
(query, params type). The scanner only runs with `params=`.

## Consequences

`params=` costs ~140 ns per call over positional parameters (`bench/named_overhead.py`);
other queries are sent exactly as before, at the same cost. t-strings stay for queries built
where their values are.
