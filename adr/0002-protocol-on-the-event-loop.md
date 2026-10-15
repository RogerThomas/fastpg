# 0002. Protocol on the event loop

## Context

A driver that runs protocol I/O on its own thread pays for hand-off syscalls, wake-ups and
runtime scheduling on every query. Under CPU quotas (containers at or below one CPU,
serverless) this dominates: a threaded design measured 47-77 µs of process CPU per
sequential lookup, against 33-35 µs for asyncpg.

## Decision

- The asyncio loop thread does all the work. Python connects the socket (numeric hosts and
  unix sockets without a thread, host names through `loop.getaddrinfo`) and registers it
  with `loop.add_reader` / `add_writer`.
- A per-connection `Executor` (`native/src/event_loop.rs`) polls xitca-postgres's futures on
  the loop thread. `LoopSocket` implements xitca's I/O trait with readiness fed by those
  callbacks. tokio is used only for its runtime-free `sync` primitives and `select!`: no
  runtime starts, fastpg starts no threads, and nothing busy-polls.
- A waker fired on another thread (for example during garbage collection) writes one byte
  to a per-connection socketpair that the loop watches (`Poker`).
- `EofWatch` (`native/src/net.rs`) wraps the stream. A zero-byte read or any fatal I/O error,
  such as `ECONNRESET` on Linux, marks the connection dead, and every pending and later
  operation fails at once with `ConnectionClosedError`. TLS runs inside the watch.

## Consequences

- Process CPU per sequential lookup is 26-28 µs. One pipelined connection did 149k ops/s
  under a one-CPU limit.
- A connection is bound to the loop that opened it; it must not be used from another
  thread or after `fork()`.
- Windows is unsupported ([ADR 0011](0011-scope-and-platform.md)).
