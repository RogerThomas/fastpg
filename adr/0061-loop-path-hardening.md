# 0061. Harden the loop path

- Status: Accepted
- Date: 2026-10-10

## Context

Before loop-native I/O became the only path, its gaps had to be closed. Details are in
[notes/inline-hardening.md](../notes/inline-hardening.md).

## Decision

- COPY back-pressure waits on socket progress only; the 50 ms re-check timer, the last user
  of a tokio runtime, is removed.
- `connect()` resolves numeric hosts with `AI_NUMERICHOST` and does its own non-blocking
  connect: `loop.getaddrinfo` always uses the default executor's thread, and uvloop's
  `sock_connect` starts four libuv threads for any TCP address. Host names still go through
  `loop.getaddrinfo`.

## Consequences

`tests/test_loop_io_stress.py` covers 64 MiB values, a 1M-row `copy_in`, 200 connections on
one loop, cancellation storms, a server kill during a large COPY, and a Linux-only check
that fastpg starts no thread under asyncio and uvloop (`tests/thread_probe.py`).
