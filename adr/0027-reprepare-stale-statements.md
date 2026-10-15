# 0027. Re-prepare stale statements once

- Status: Accepted
- Date: 2026-10-10

## Context

Cached prepared statements go stale after DDL (`0A000 cached plan must not change result
type`) or after `DEALLOCATE ALL` / `DISCARD ALL` (`26000`).

## Decision

Re-prepare and retry once, transparently, matching asyncpg. This happens only when no rows
have been delivered yet; both errors occur at Bind, before execution.

## Consequences

Schema changes and session resets do not break cached queries, and a retry can never
duplicate delivered rows.
