# 0057. Loop-native I/O is the only I/O path

- Status: Accepted
- Date: 2026-10-10

## Context

[0056](0056-loop-native-driver.md) planned loop-native I/O. The threaded path would only be kept for a transition
release if loop pipelining lagged behind it.

## Decision

The asyncio loop thread connects the socket (in Python), watches it with `add_reader` /
`add_writer`, and polls xitca-postgres's futures itself (`native/src/event_loop.rs`). fastpg
starts no threads. The threaded path, the `io=` option, `FASTPG_IO` and the `fastpg-thread*`
bench adapters are deleted, since nothing had been released.

Linux, 16 concurrent tasks, median of 3, ops/s, loop vs thread:

| CPU limit | pipelined, one connection | pool of 16 | asyncpg |
|---|---:|---:|---:|
| none | **154.2k** vs 122.6k | 70.3k vs **75.4k** | 25.2k |
| `cpus: 1` | **149.1k** vs 85.7k | **70.5k** vs 50.7k | 25.0k |
| `cpus: 0.5` | **65.8k** vs 33.7k | **32.9k** vs 20.3k | 11.6k |

## Consequences

Loop pipelining is 1.22-1.99x the threaded path. The thread was ahead only for
non-pipelined concurrency with no CPU limit. With no limit, sequential lookups did 15.27k
ops/s (thread 9.81k, asyncpg 13.57k). Raw logs are in
[notes/eval/2026-10-loop-native/](../notes/eval/2026-10-loop-native/).
