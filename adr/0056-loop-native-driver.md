# 0056. Move to a loop-native driver

- Status: Accepted
- Date: 2026-10-10

## Context

Under CPU quotas (Kubernetes at or below 1 CPU, Lambda), fastpg lost to asyncpg by 1.5-4x
on sequential point lookups on Linux, because it cost more total CPU per request (~51 µs vs
29-44 µs; spinning made it worse). The cause was the second thread: hand-off syscalls,
wake-ups and tokio scheduling.

## Decision

The asyncio loop owns the PostgreSQL socket, and Rust encodes, parses and decodes
synchronously on that thread. Drive xitca-postgres's futures from the loop if that is clean;
otherwise use `postgres-protocol` for message encoding and parsing with an own connection
state machine. The spin module ([0067](0067-io-thread-busy-poll.md)), the cross-thread mailbox and the tokio runtime are
to be deleted. Alternatives (adaptive spin, loop-thread submit, other primitives) were
stopped as moot.

## Consequences

Implemented in [0057](0057-loop-native-only-io-path.md)-[0059](0059-tokio-without-runtime.md): the xitca route worked, so there is no `postgres-protocol`
state machine. The mailbox stays as a completion queue on the loop thread plus the
off-thread `Poker` ([0059](0059-tokio-without-runtime.md)).
