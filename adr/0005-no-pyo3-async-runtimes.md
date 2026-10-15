# 0005. No pyo3-async-runtimes; Python objects only on the loop thread

- Status: Partly superseded by [0057](0057-loop-native-only-io-path.md)
- Date: 2026-10-10

## Context

`pyo3-async-runtimes` resolves every future by acquiring the GIL on the tokio thread. Under
load that contends with the event loop, and its per-call overhead showed up in prior art.

## Decision

fastpg does not use `pyo3-async-runtimes`. I/O tasks never touch Python: they copy raw column
bytes into an arena and post to a per-connection mailbox. The mailbox wakes the asyncio loop
through a socketpair registered with `loop.add_reader`, and the loop thread drains every
completion at once, decoding rows and resolving futures. Python objects are only created on the
loop thread.

Since [0057](0057-loop-native-only-io-path.md) there is no I/O thread: the loop thread performs the I/O itself. Not using
`pyo3-async-runtimes`, the loop binding and the Unix-only restriction still hold.

## Consequences

- A connection is bound to the event loop that opened it.
- Unix only (macOS and Linux). Windows is not supported because of the socketpair and
  `add_reader`.
