# 0067. Busy-poll the I/O thread after each query

- Status: Superseded by [0058](0058-delete-spin-busy-poll.md)
- Date: 2026-10-10

## Context

On Linux with a low-latency network, a parked I/O thread ([0006](0006-single-tokio-io-thread.md)) costs a ~35 µs scheduler
wake-up per query.

## Decision

The I/O thread busy-polls for 200 µs after each query by default (`FASTPG_SPIN_US`; `0`
disables). Sequential point lookups went from 98 µs to 50 µs p50 (asyncpg: 72 µs), from
0.72x to 1.43x asyncpg.

## Consequences

Under steady sequential load one core stays busy, and with sporadic traffic each query burns
up to 200 µs of extra CPU. Superseded by [0058](0058-delete-spin-busy-poll.md): the spin and `FASTPG_SPIN_US` are deleted.
