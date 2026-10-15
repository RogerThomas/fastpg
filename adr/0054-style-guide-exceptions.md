# 0054. Style guide applied repo-wide, with justified exceptions

- Status: Accepted
- Date: 2026-10-10

## Context

[style-guide.md](../style-guide.md) applies to the whole repository, but a few of its
"prefer" rules do not fit every case.

## Decision

Apply the guide repo-wide, keeping:

- `Transaction` as a plain slotted class: its constructor computes state from options;
- justified `noqa`s for ruff rules (e.g. `S608` on SQL built from quoted fixture names);
- three commented pyright suppressions in the benchmark drivers for libraries without type
  hints: the pg8000 import and one `cast`, and psycopg2's unannotated `register_uuid`.

## Consequences

No suppressions remain in the library or tests.
