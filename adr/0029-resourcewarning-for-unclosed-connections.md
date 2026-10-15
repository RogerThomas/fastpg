# 0029. ResourceWarning for unclosed connections

- Status: Accepted
- Date: 2026-10-10

## Context

Connections that are garbage-collected without `close()` leak a session and a registered fd.

## Decision

Emit a `ResourceWarning` when an unclosed connection is garbage-collected, then clean up:
close the session and unregister the fd. This matches asyncio transports and asyncpg.

## Consequences

Leaks are visible under `-W error::ResourceWarning` while still being cleaned up.
