# Inline mode hardening: pool, zero extra threads, stress tests

> **History.** Inline mode became the only I/O path afterwards ([ADR 0002](../adr/0002-protocol-on-the-event-loop.md)): there
> is no `inline=` option, no threaded path, no spin and no `FASTPG_IO`, and the pool always
> opens loop-native connections. File names below are updated to the current ones
> (`native/src/event_loop.rs`, `tests/test_loop_io_stress.py`, `tests/thread_probe.py`,
> `test_starts_no_threads`); the rest is unchanged.

Follow-up to `notes/latency-c.md` (the inline prototype). Branch `inline-hardening`, based
on the prototype branch. Goal: close the gaps that block loop-native becoming the only
architecture. No performance benchmarks were run here.

## 1. What changed

| area | change | files |
|---|---|---|
| pool | `create_pool(dsn, ..., inline=True)` (and a read-only `Pool.inline` field) opens every connection with `connect(..., inline=True)`: initial `min_size` connections, growth, replacement of dead / expired / idle connections | `src/fastpg/_pool.py` |
| no runtime | `Executor::run` no longer enters the process-wide tokio runtime; inline COPY back-pressure waits on socket progress only (section 2) | `native/src/event_loop.rs`, `native/src/bulk.rs`, `native/src/connection.rs` (`Shared::is_inline`) |
| connect | numeric hosts are converted with `getaddrinfo(..., AI_NUMERICHOST)` instead of `loop.getaddrinfo` (which always runs in the loop's default executor, i.e. starts a thread); the socket is connected with a plain non-blocking `connect` + `add_writer` instead of `loop.sock_connect` (uvloop's resolves every TCP address again through libuv's thread pool: four threads) | `src/fastpg/_connection.py` (`_resolve`, `_sock_connect`) |
| tests | every pool test runs twice (`mode` fixture: threaded / inline) plus a new concurrent pool bulk-write test; new `tests/test_loop_io_stress.py` (section 4); `tests/thread_probe.py` (the thread-count probe, run in a fresh interpreter) | `tests/` |

The default (threaded) path is unchanged: same tokio runtime, same spin, same 50 ms COPY
re-check timer.

## 2. How COPY back-pressure works now

`copy_in` hands encoded chunks to xitca's (unbounded) write buffer with `send_one_way`,
then `wait_for_socket` holds the chunk's ack until at most 4 MiB
(`COPY_HIGH_WATER`) of what it handed over is still unwritten. "Written" is
`IoWatch::written()`, counted by the `EofWatch` socket wrapper; `IoWatch::progress` is
notified after every successful write and on EOF / fatal I/O errors (`signal_eof`).

Inline mode, step by step:

1. The COPY task appends a chunk; `send_one_way` wakes the driver task (same executor,
   same loop thread, no syscall).
2. The driver writes until the kernel buffer is full: `LoopSocket::write` gets
   `WouldBlock`, clears `writable` and sets `want_write`. Each successful write notifies
   `progress`.
3. The COPY task, still over the high-water mark, registers on `progress.notified()` and
   returns `Pending`. **No timer**: in inline mode it waits on the notification alone.
4. At the end of the pump `Inline::sync_hooks` sees `want_write` and calls
   `loop.add_writer(fd, native.on_writable)`.
5. When the kernel drains, asyncio calls `on_writable`: `writable` is set, the driver's
   write waker fires, the pump runs the driver, which writes (notifying `progress`), which
   wakes the COPY task; once below the mark it posts the ack and Python feeds the next
   chunk. `sync_hooks` removes the writer again once nothing wants it.

Why the 50 ms timer isn't needed inline: it existed to re-check `client.closed()` should
the connection die mid-wait. A dying connection either signals EOF/fatal error (which
notifies `progress` and ends the driver) or ends the driver; either way `serve` sets
`dead`, and `DeadWatch` (`Shared::unless_dead`) drops the whole COPY task. Nothing else
can stall the driver's writes. `test_copy_in_fails_promptly_when_server_closes` (inline)
covers this: `pg_terminate_backend` 0.3 s into a 50M-row COPY fails it within ~0.05 s.

The threaded path keeps the timer (as a belt-and-braces re-check; it costs nothing
there because the runtime exists anyway). When loop-native becomes the only path the
`is_inline()` branch and the timer go away together.

## 3. Thread-count proof

Static check, every runtime touchpoint in the inline path:

| grep hit | inline? |
|---|---|
| `runtime::get().spawn` (`Shared::spawn`, `Connection::connect`) | no: `Shared::spawn` uses the executor in inline mode; `connect` is the threaded entry point |
| `runtime::spin::touch` → `tokio::spawn` (`Shared::touch`) | no: `touch` is a no-op inline |
| `tokio::time::timeout` (`bulk::wait_for_socket`) | no: skipped when `shared.is_inline()` |
| `runtime::get().enter()` (`Executor::run`) | **removed** |
| `tokio::net::lookup_host`, `TcpStream::connect` (`net::connect`) | threaded path only |
| `tokio::task::yield_now` (`event_loop.rs` unit test) | test only; works without a runtime |
| xitca: `cancel.rs` `Handle::try_current`, `execute/async_impl.rs` `spawn_blocking`, `driver.rs` `lookup_host`, `proxy.rs` / `transaction/builder.rs` `tokio::spawn` | `query_cancel` / SQL-file execution / `Postgres::connect` / quic proxy / unit tests: none used by fastpg |
| `tokio::sync::{mpsc, Notify, RwLock, watch}`, `tokio::select!` | waker-based, no runtime needed |

Dynamic check: `tests/test_loop_io_stress.py::test_starts_no_threads` (Linux only; it reads
`/proc/self/task`) runs `tests/thread_probe.py` in a fresh interpreter, because a
runtime started by an earlier test lives for the whole process. The probe connects, runs
sequential, pipelined (200) and streamed (20k rows) queries, a 200k-row `copy_in` (~25 MB,
far past the high-water mark, so the back-pressure wait runs), `insert_many`,
`execute_many`, a transaction, and closes; it reports the thread count before the work and
the highest count at five checkpoints. Parametrised over asyncio and uvloop, and over
inline and threaded mode: inline must stay flat, threaded (the control) must grow, which
shows the probe can see a thread being started.

Results:

| loop | mode | macOS (`ps -M`, scratch variant of the probe) | Linux (Docker run) |
|---|---|---|---|
| asyncio | inline | 1 → 1 | flat (passed) |
| uvloop | inline | 1 → 1 (was 1 → 5 before `_sock_connect`) | flat (passed) |
| asyncio | threaded | 1 → 2 | grows (passed) |
| uvloop | threaded | 1 → 2 | grows (passed) |
| asyncio | inline, `Executor::run` entering the runtime again (mutation check) | 1 → 2 | n/a |
| asyncio | inline, DSN host `localhost` | 1 → 2 (asyncio's default executor resolving the name) | n/a |

The last row is expected: a host *name* has to be resolved, and asyncio does that in its
default executor (uvloop: libuv's pool, four threads). Numeric hosts and unix sockets need
no thread. The Linux test therefore resolves the compose host name (`postgres`) in the
parent before handing the DSN to the probe.

## 4. Stress and edge tests (inline)

`tests/test_loop_io_stress.py`, host timings (macOS, Docker Desktop PostgreSQL):

| test | what it checks | time |
|---|---|---:|
| `test_64_mib_bytea_parameter_roundtrip` | 64 MiB parameter inserted (write `WouldBlock` → `add_writer`), a small query pipelined behind it, server-side `length`/`md5` match, value selected back byte-identical (multi-read response) | 1.1 s |
| `test_copy_in_a_million_rows` | 1,000,000-row `copy_in` from a generator; count / sum / max | 0.2 s |
| `test_200_connections_on_one_loop` | 200 inline connections opened concurrently on one loop, 10 pipelined queries each, one backend per connection, all closed | 0.3 s |
| `test_cancellation_storm[before-reply]` | 1,000 queries on one connection, all cancelled before any reply; the connection still works, `cached_statements` is 2, live `asyncio.Future` objects back to the baseline (no request leaked) | 0.04 s |
| `test_cancellation_storm[mid-replies]` | same, cancelled 5 ms in (some completed, results correct) | 0.04 s |
| `test_pool_cancellation_storm` | 1,000 pool queries (`max_size=2`) cancelled; in-flight ghosts are flushed, `stats()` returns to idle, pool still works | 0.02 s |
| `test_copy_in_fails_promptly_when_server_closes` | `pg_terminate_backend` during a large inline COPY: fails with `OperationalError`/`ConnectionClosedError`, connection closed, no rows | 0.4 s |
| `test_no_busy_loop_after_server_closes[idle / in-flight]` | process CPU over 1 s after the server killed the session is < 0.1 s | 1.1 s each |

The pool's inline runs (`tests/test_pool.py`, `[inline]` ids) cover shared routed
queries, routing and caps, `acquire()`, `transaction()` / savepoints / options, session
reset, cancellation, the new pool bulk-write test, replacing terminated connections,
in-flight failure, unreachable server, graceful close, leaks, `max_lifetime` and
`max_idle`. All pass in both modes (81 pool tests).

### The EOF busy-loop question

`LoopSocket` clears `readable` only on `WouldBlock`, so after a 0-byte read (EOF) the flag
stays set, and asyncio's reader registration is level-triggered: a socket at EOF is
readable forever. It doesn't spin, because of an invariant rather than the flag: `EofWatch`
signals EOF on the 0-byte read (or a fatal error), `serve` drops the driver and sets
`dead` in the same executor run, and the same pump's `sync_hooks(dead)` calls `detach`,
which removes the reader and writer and closes the socket. Measured: 0.0001 s of process
CPU in the second after the server closed an inline connection (same as a healthy idle
connection). Mutation check: with the `dead` branch of `sync_hooks` disabled (reader
left registered), the test fails with 1.0 s of CPU in 1 s. So no fix was needed; the test
now guards the invariant. (Clearing `readable` on EOF would be wrong: xitca's `try_read`
treats a read that got data and then 0 bytes as "data", and only marks the read side
closed on the next readiness, which would then never come.)

## 5. Auto-selection when `available_parallelism() < 2`: recommendation

**Don't auto-select inline mode now; make it the default outright once the Linux numbers
confirm it, and in the meantime switch the spin off on one CPU.**

Reasons:

* The plan is that loop-native becomes the only path (`latency-c.md` section 7). A
  heuristic would be temporary code whose main effect is that the same application runs
  two different I/O architectures on a laptop and in a 1-CPU pod, so bugs and performance
  reports stop being reproducible across environments.
* `available_parallelism()` is a weak proxy for "one CPU": it honours cgroup quotas and
  affinity, but AWS Lambda reports the sandbox's vCPUs rather than the memory-proportional
  share, and a Kubernetes CPU *request* without a limit sets no quota at all. It would miss
  several of the environments it targets.
* What actually hurts on one CPU is the spin (the tokio thread busy-polling takes the only
  core away from the loop: spin 200 at `--cpus=0.25` was slower than spin 0, lead's data),
  not the second thread as such. Defaulting `FASTPG_SPIN_US` to 0 when
  `available_parallelism() < 2` removes that harm with no semantic change, and it is a
  one-line, low-risk heuristic.
* Inline mode still has semantic differences an automatic switch would impose silently:
  the GIL is held during socket I/O and protocol work; host names are resolved in the
  loop's executor; it is experimental and newly hardened.
* If an interim switch is wanted anyway, make it explicit: `FASTPG_IO=inline|thread`
  (environment) next to `connect(inline=...)` / `create_pool(inline=...)`, so operators of
  1-CPU deployments opt in deliberately.

## 6. Linux run

`docker compose -f <checkout>/docker-compose.yml --profile test run --rm --build --no-deps
tests`. `--no-deps` was added to the documented command: a second checkout's compose file
has different absolute bind-mount paths from the main checkout's, so letting compose manage
the `postgres` dependency could have recreated the shared container; the tests container
joins the existing `fastpg_default` network either way (the `postgres` container was
left untouched: same uptime before and after).

Result: cargo tests 12 passed; pytest **424 passed, 0 skipped** in 21 s, i.e. including
the four `test_starts_no_threads` cases (the Linux test asserts; it doesn't print the counts),
all inline stress tests and both modes of every pool test. Host (macOS): 420 passed, 4
skipped (the thread-count cases).

## 7. Remaining gaps

* **Name resolution** of non-numeric hosts uses asyncio's default executor (stdlib) or
  libuv's pool (uvloop). Zero threads holds for numeric hosts and unix sockets only. An
  async resolver would be a new dependency; not worth it before the migration.
* **The mailbox socketpair** is still created per inline connection (two fds and a
  second `add_reader`), only for off-thread wakes (`__del__` from a foreign thread).
  Migration step 2 in `latency-c.md` removes it; until then 200 inline connections use 600
  fds.
* **The GIL is held** during inline socket I/O and protocol parsing (unchanged).
* **No server-side cancel**: the cancellation storm leaves the 1,000 cancelled queries
  running on the server until they finish (cheap here; a long query keeps its connection
  busy, as on the threaded path).
* **`_sock_connect` on a unix socket** follows asyncio's own semantics: a `BlockingIOError`
  from `connect` (EAGAIN on a full listen backlog) is waited out with `add_writer` and then
  checked through `SO_ERROR`, exactly as `loop.sock_connect` does.
* **Not measured**: the per-pump `add_writer`/`remove_writer` toggling during large writes
  (an `epoll_ctl` each), and the 64 MiB / 1M-row timings on Linux. Benchmarks were left
  to separate runs.
* The thread-count test is Linux-only (it needs `/proc/self/task`); macOS was checked by
  hand with `ps -M` (section 3).
