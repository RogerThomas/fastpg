# 0006. One tokio I/O thread by default

- Status: Superseded by [0057](0057-loop-native-only-io-path.md) and [0059](0059-tokio-without-runtime.md)
- Date: 2026-10-10

## Context

The original design ran protocol I/O on a tokio runtime, so it needed a thread count.

## Decision

Use one tokio I/O thread by default, overridable with `FASTPG_IO_THREADS`.

## Consequences

1, 2 and 4 threads measured the same (see [perf-notes.md](../perf-notes.md)). Since
[0057](0057-loop-native-only-io-path.md) and [0059](0059-tokio-without-runtime.md) there is no I/O thread and no tokio runtime, so the setting no longer
exists.
