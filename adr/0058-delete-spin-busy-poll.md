# 0058. Delete the spin busy-poll

- Status: Accepted
- Date: 2026-10-10

## Context

The spin ([0067](0067-io-thread-busy-poll.md)) hid the I/O thread's wake-up latency. With no I/O thread ([0057](0057-loop-native-only-io-path.md)) there is
nothing to hide, and under CPU quotas the spinning thread uses the quota the loop thread
needs.

## Decision

Delete the spin and `FASTPG_SPIN_US` rather than defaulting it to 0. Sequential point
lookups, ops/s:

| CPU limit | loop-native | asyncpg | thread, spin 0 | thread, spin 200 |
|---|---:|---:|---:|---:|
| none | **15.4k** | 14.3k | 9.8k | 18.0k, at 126% CPU |
| `--cpus=0.5` | **14.6k** | 13.4k | 9.2k | 6.0k |
| `--cpus=0.25` | **9.2k** | 7.2k | 4.8k | 1.7k |

## Consequences

Process CPU per request: loop-native 26-28 µs, asyncpg 33-35 µs, threaded 47-77 µs. Spin 200
won only with no limit or a dedicated core, at the cost of a busy core, and even then its
open-loop p99 at 10k req/s was 4.7 ms against loop-native's 262 µs.
