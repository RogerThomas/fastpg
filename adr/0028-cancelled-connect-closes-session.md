# 0028. A cancelled connect does not leak a session

- Status: Accepted
- Date: 2026-10-10

## Context

Cancelling `connect()` while the handshake is in flight could leave an open server session
with no owner.

## Decision

A `closing` flag makes a connect that is still in flight close itself when it completes.

## Consequences

Cancellation and timeouts around `connect()` do not leak server sessions.
