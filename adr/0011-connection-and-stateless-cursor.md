# 0011. Connection plus stateless Cursor API

- Status: Partly superseded by [0062](0062-queries-on-connection.md)
- Date: 2026-10-10

## Context

The initial specification used `cursor.select_one(...)`.

## Decision

`await fastpg.connect(dsn)` returns a `Connection`; `conn.cursor()` returns a `Cursor` that
runs the queries. The cursor holds no state: no server-side portal and no `fetchmany`.

## Consequences

The cursor half is reversed by [0062](0062-queries-on-connection.md): queries now run on the `Connection` itself and
`Cursor` is no longer public.
