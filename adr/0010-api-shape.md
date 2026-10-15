# 0010. API shape

## Context

DB-API 2.0 centres on stateful cursors, `fetch*` methods and untyped rows. fastpg's purpose
is typed rows in asyncio applications, which fits that model poorly, and a cursor that
holds no server-side state is one more object with no function.

## Decision

- The query methods (`select_one`, `select_one_or_none`, `select_many`, `execute`,
  `execute_script`, `execute_many`, `insert_many`, `copy_in`) are defined once on a private
  base that `Connection` and `Transaction` inherit; `Pool` offers the same methods. There
  is no public cursor.
- `conn.transaction()` and `pool.transaction()` give a `Transaction` that commits on
  success, rolls back on exception and nests with `savepoint()`. COMMIT of an aborted
  transaction raises (SQLSTATE 25P02) rather than rolling back silently.
- The API is deliberately not DB-API compliant: no stateful cursor, typed `into=` results
  and `$1` placeholders.
- An unclosed connection that is garbage-collected emits a `ResourceWarning` and is closed.

## Consequences

- The same call works on a connection, a transaction or a pool, and typed overloads follow
  `into=` with no delegation cost per query.
- The API reference lists inherited methods.
- Tools that expect DB-API, such as SQLAlchemy, need an adapter. One is possible later and
  can reuse the PEP 249 exception names ([ADR 0009](0009-errors.md)).
