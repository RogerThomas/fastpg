# 0047. t-string rules

- Status: Accepted
- Date: 2026-10-10

## Context

t-string support ([0045](0045-t-string-parameters.md)) needs precise rules; details are in
[notes/templates.md](../notes/templates.md).

## Decision

- Values are always bound as `$n`.
- Conversions, format specs and literal `$<digit>` in template text are rejected
  (`ValueError`).
- `fastpg.Identifier` handles quoted names.
- `execute_script` accepts templates only when they produce no parameters, because DDL cannot
  take bind parameters.
- No separate Template-only overloads, to keep the API small.
- `execute_many` does not take templates: its rows are data, and templates are one-shot.

## Consequences

The conservative `$n` rule rejects some valid text; the workaround is to pass such text as a
value.
