# 0062. Queries run on the Connection; Cursor is private

- Status: Accepted
- Date: 2026-10-11

## Context

The cursor from [0011](0011-connection-and-stateless-cursor.md) never held state (no portal, no `fetchmany`): one more object to
explain and nothing else.

## Decision

`conn.select_one(...)` replaces `conn.cursor().select_one(...)`, the same call as
`pool.select_one(...)` and `tx.select_one(...)`. `Connection.cursor()` is deleted and
`Cursor` is not exported. The query methods move unchanged to a private base, `_Queries`,
that `Connection` and `Transaction` subclass: delegation would repeat every overload and add
a call per query.

## Consequences

The pool calls the methods on `_Entry.conn`, so a shared statement costs what it did. The
API reference lists inherited methods (`inherited_members` in `docs/api.md`;
`scripts/build_docs.py` folds private bases into the Markdown export).
