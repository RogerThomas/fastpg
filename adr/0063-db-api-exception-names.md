# 0063. DB-API 2.0 exception names

- Status: Accepted
- Date: 2026-10-11

## Context

fastpg already had classes in PEP 249's places, but not all of them were exported, and
feature-not-supported errors had no class.

## Decision

Export `InterfaceError`, `DatabaseError`, `DataError`, `OperationalError`,
`IntegrityError`, `InternalError` and `ProgrammingError`. `Error` is an alias of
`FastPGError`, not a rename. Add `NotSupportedError(DatabaseError)` for SQLSTATE class 0A.
No `Warning`: fastpg raises none and the name would shadow the builtin. fastpg's own classes
stay: `ResultError` under `Error`, `TransactionError` (class 40) under `DatabaseError`,
`ConnectionClosedError` under `InterfaceError`, also when the server drops the connection.

## Consequences

Existing handlers keep working. The rest of the API is deliberately not DB-API (no stateful
cursor, `into=` rows, `$1` placeholders), but a DB-API adapter, e.g. for SQLAlchemy, could
reuse these names.
