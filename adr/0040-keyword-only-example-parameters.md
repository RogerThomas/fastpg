# 0040. Keyword-only parameters in examples

- Status: Accepted
- Date: 2026-10-10

## Context

`examples/basic.py` takes its DSN through yeetr's `Opt(envvar="FASTPG_DSN")`, which yeetr
only accepts on keyword-only parameters; a positional version raises `YeetrError` at
startup.

## Decision

The example's `main` takes keyword-only parameters (`*,`).

## Consequences

The example starts correctly and matches the `--person-id 42` usage in its docstring.
