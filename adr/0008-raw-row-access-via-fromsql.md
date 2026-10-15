# 0008. Raw row access through a private FromSql type

- Status: Accepted
- Date: 2026-10-10

## Context

xitca-postgres 0.4 has no public raw-bytes accessor, only typed `try_get::<T: FromSql>`.
fastpg decodes columns itself and needs the raw bytes. xitca's `get_zc` path reports empty
non-NULL values such as `''` as NULL.

## Decision

Implement `FromSql` for a private `RawCell` type that accepts every PostgreSQL type, and read
cells through it. `get_zc` is not used. xitca does all protocol work, always in binary
format; there is no custom wire protocol.

## Consequences

Empty values and NULL stay distinct. fastpg depends on xitca's `FromSql` contract rather
than on internals.
