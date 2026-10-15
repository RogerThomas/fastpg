# 0059. tokio without a runtime

- Status: Accepted
- Date: 2026-10-10

## Context

After [0057](0057-loop-native-only-io-path.md) fastpg only uses tokio's `RwLock`, `watch`, `Notify`, `mpsc` and `select!`,
which are waker-based and need no runtime.

## Decision

Trim tokio to the `sync` and `macros` features and delete `runtime.rs`. Keep the `Poker`: a
waker can fire on another thread (e.g. a `__del__` run there by the garbage collector drops
a client and wakes the driver). The executor queues the task and the `Poker` writes one byte
to the connection's socketpair, which the loop watches with `add_reader`.

## Consequences

Queries never use the `Poker`; the completion queue (`mailbox.rs`) is otherwise filled and
drained on the loop thread with no wake-up. xitca-postgres still enables tokio's `rt` and
`net` for its unused connect path, so they compile, but nothing starts a runtime.
