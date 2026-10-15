# 0050. Detect open transactions with a probe

- Status: Accepted
- Date: 2026-10-10

## Context

Bulk writes ([0048](0048-bulk-writes.md)) choose between `BEGIN` and `SAVEPOINT`, so they must know whether a
transaction is open. The clean source is the transaction status in ReadyForQuery, but xitca
does not expose that byte.

## Decision

Detect an open transaction with a side-effect-free probe:
`set_config('fastpg.in_transaction', ..., is_local)`.

## Consequences

Known wart: the placeholder setting shows up in `SHOW ALL`. Revisit if it causes problems or
xitca exposes the status byte.
