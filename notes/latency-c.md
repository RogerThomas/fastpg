# Attempt C: inline mode (zero thread hops), and the road to a loop-native fastpg

> **History.** This is the design note for the loop-native prototype ("inline mode", attempt
> C). It became the only I/O path: see [ADR 0002](../adr/0002-protocol-on-the-event-loop.md). The
> `inline=` option, the threaded path, `FASTPG_SPIN_US`, `runtime.rs` and the
> `fastpg-inline` / `fastpg-struct` (threaded) bench adapters it compares no longer exist;
> `inline.rs` is now `native/src/event_loop.rs`, and the tests are `tests/test_loop_io.py`,
> `tests/test_loop_io_stress.py` and `tests/thread_probe.py`. The text below is unchanged
> apart from those file names.

Branch work for `fastpg.connect(dsn, inline=True)`: the asyncio event-loop thread drives
xitca-postgres itself, so a query crosses no thread boundary. Code: `native/src/event_loop.rs`
(executor + loop-driven `AsyncIo`), `native/src/connection.rs` (`Inline`, `attach`, `pump`),
`native/src/net.rs` (`config`, `targets`, `handshake` reused), `native/src/mailbox.rs`
(`Poker`, quiet pushes), `src/fastpg/_connection.py` (`connect(inline=True)`,
`_open_socket`). Tests: `tests/test_loop_io.py`. Measurement: `bench/cpu_per_request.py`,
`bench/drivers/fastpg.py` (`fastpg-inline`).

**Verdict: feasible and clean (route 1).** xitca-postgres 0.4 can be driven entirely from
the asyncio thread through its public `Postgres::connect_io` + `AsyncIo` seam, with no
patching, no vendoring and no custom wire protocol. The prototype passes a focused inline
test file (select/execute shapes, errors, NULLs, Structs in all three strategies, 20k-row
streaming, pipelining, transactions, `execute_many`, `copy_in`, cancellation, server
termination with and without in-flight queries, cancelled connect, bad credentials,
unreachable server, uvloop) and the whole existing suite (373 tests) with the default path
unchanged.

## 1. Feasibility: what xitca needs from a runtime

Everything below refers to `~/.cargo/registry/src/*/xitca-postgres-0.4.0/src` and
`xitca-io-0.5.1/src` (`tokio 1.53`).

What xitca needs from the I/O type (`xitca-io/src/io.rs`, trait `AsyncIo`):
`io::Read + io::Write + Unpin`, `ready(interest) -> impl Future<Output = io::Result<Ready>>
+ Send`, `poll_ready`, `is_vectored_write`, `poll_shutdown`. `Postgres::connect_io<Io:
AsyncIo + Send + 'static>` (`lib.rs:166`) boxes it as `Box<dyn AsyncIoDyn + Send>`
(`driver.rs:72`), so the driver is `GenericDriver<Box<dyn AsyncIoDyn + Send>>`. `Interest`
and `Ready` are tokio's bitflag types re-exported by xitca-io; constructing them needs no
runtime.

How the driver uses it (`driver/generic.rs`): `_try_next` (line ~200) alternates between
`io.ready(READABLE).select(rx.wait())` and `io.ready(READ|WRITE)` depending on its state;
`try_read` reads until `WouldBlock`, `try_write` writes until the buffer is empty or
`WouldBlock`. `rx.wait()` is a `poll_fn` over a `Mutex<State>` with a stored `Waker`
(`SharedState::wait`, line ~96): submitting a query (`DriverTx::_send`, line ~70) appends to
the buffer and calls `waker.wake()`. All plain `std::task` machinery. The `select` is
`xitca_unsafe_collection::futures::Select` (no runtime).

Hidden runtime dependencies, each checked:

| where | what | effect on a loop-driven design |
|---|---|---|
| `driver/codec.rs:9` | `tokio::sync::mpsc::unbounded_channel` for per-request responses | none: tokio's sync primitives are waker-based and work without a runtime (verified by running them on the loop thread) |
| `driver.rs:145` `dns_resolve` | `tokio::net::lookup_host` | only on `Postgres::connect`, not `connect_io`; we connect from Python |
| `driver/connect.rs` | `TcpStream::connect` (tokio) | same: `connect` path only |
| `execute/async_impl.rs:171-213` | `tokio::task::spawn_blocking` reading SQL files | only for the `ExecuteFile`-style path; fastpg never uses it |
| `cancel.rs:29` | `Handle::try_current()` else builds a `current_thread` runtime | only for `Session::query_cancel`; fastpg has no server-side cancel today (if added, implement it over our own socket) |
| `proxy.rs`, `pool/*` | `tokio::spawn` | quic proxy / xitca's own pool; unused |
| `Cargo.toml` | tokio features `rt`, `sync` | compile-time only; no runtime is started by the inline path |

And in fastpg's own code: `runtime::get().spawn` (query, script and bulk tasks),
`runtime::spin::touch()` (`tokio::spawn`), and `tokio::time::timeout` in
`bulk::wait_for_socket` (COPY backpressure re-check). The first two are routed through new
`Shared::spawn` / `Shared::touch`, which pick the loop executor and skip spinning in inline
mode. The timer still needs a tokio runtime *context*: `Executor::run` wraps every poll in
`runtime::get().enter()`, so a timer registered by COPY fires on the (otherwise idle) tokio
thread and reaches the loop as an off-thread wake (section 2). For the loop-native
migration that timeout is simply removed (section 7).

`tokio::select!`, `tokio::sync::{RwLock, watch, Notify}` used in `connection.rs`/`bulk.rs`
all work without a runtime, as does xitca's `Statement::named(..).execute()` (prepare +
type lookups, `prepare.rs`).

Conclusion: no blocker. The only awkward spot is our own COPY timer, which the final design
drops anyway.

## 2. Design

```
 asyncio loop thread (the only thread that touches the connection)
 ──────────────────────────────────────────────────────────────────
 connect(dsn, inline=True)
   Python: loop.getaddrinfo + loop.sock_connect  → non-blocking socket (Python owns the fd)
   native.attach(fd, ...)                         → LoopSocket{fd, IoState} wrapped in EofWatch
   loop.add_reader(fd, native.on_readable)        → Postgres::connect_io(socket) spawned on
                                                    the connection's Executor; pump()
 cursor.select_one(query, 42, into=Person)
   params → Rust (GIL held), statement-cache lookup
   Shared::spawn(run(..)) on the Executor; pump():
     run():  poll woken tasks: query task → DriverTx::send (bytes in xitca's buffer, wakes
             the driver task) → driver task → poll_ready(WRITABLE) is Ready (flag) → write()
             → poll_ready(READABLE) Pending (registers waker)
     drain(): nothing yet
   ... loop sleeps in kqueue/epoll on *this* socket ...
   on_readable(): IoState.readable = true, wake read waker; pump():
     run():  driver task reads until WouldBlock (flag cleared), try_decode routes bytes to
             the query's response channel → query task parses rows into RowBuffer, posts
             Outcome::Rows (quiet push) and finishes
     drain(): decode rows → Python objects, future.set_result(...)
```

* **`LoopSocket`** (`event_loop.rs`) implements `AsyncIo` over a non-blocking fd. Readiness is a
  pair of level-ish flags: `on_readable` sets `readable`, a `read()` that returns
  `WouldBlock` clears it; `writable` is assumed true until a `write()` returns `WouldBlock`,
  which also sets `want_write`. `poll_ready` answers from the flags or parks the waker.
  asyncio's `add_reader` is permanent; `add_writer` is only registered while `want_write`
  holds (`Inline::sync_hooks` toggles it after every pump through `functools.partial`s made
  in Python), otherwise an always-writable socket would spin the loop.
* **`Executor`** (`event_loop.rs`): a slab of `Pin<Box<dyn Future + Send>>` tasks and a queue of
  woken ids. Wakers push ids; `run()` polls until the queue is empty. Only ever run on the
  loop thread, inside a Python call, with the GIL held. A wake from another thread (COPY
  timer, a GC-triggered `__del__`) goes through `Mailbox::Poker`: one byte on the existing
  socketpair, which the loop's `drain` reader callback turns into a `pump()`.
* **`Shared::pump`** = `run(); drain(); repeat while tasks got woken` then `sync_hooks`.
  The repeat matters: resolving a completion can drop the last `Arc<Client>` (a
  `select_one` completion holds its `Arc<CachedStmt>`), whose drop queues `Terminate` and
  wakes the driver; without the loop that wake would wait for a readiness event that never
  comes (this was the one real bug found while prototyping: `close()` with an in-flight
  query hung).
* **Completions** reuse the existing `Request`/`Outcome`/`resolve` path unchanged: tasks
  still "post" completions, but in inline mode `Shared::post` is a quiet `Vec` push and the
  pump drains synchronously; the mailbox socketpair is read only when it was armed, so a
  normal inline query makes no mailbox syscall at all.
* **Everything else is reused as-is**: `run`/`run_once`/`prepare` (query execution, stale
  statement retry), `decode.rs`, `structs.rs`, `rows.rs`, `params.rs`, `stmt_cache.rs`,
  `EofWatch`/`IoWatch` (EOF and byte counting), `Shared::unless_dead` + the `dead` watch,
  and the bulk-write tasks (they spawn via `Shared::spawn`).
* **fd ownership**: Python's `socket` owns the fd; Rust wraps it in `ManuallyDrop` and never
  closes it. When the connection is dead, `sync_hooks` calls the `detach` partial, which
  unregisters the fd from the loop and closes the socket, in that order, and then drops the
  hooks (breaking the native ↔ bound-method reference cycle).

The per-query syscall profile (sequential, inline): `write` (query), `kevent`/`epoll_wait`
(asyncio's own), `read` (response), `read` → `EAGAIN` (xitca reads until `WouldBlock`).
The default path adds, per query: the tokio wake-up (futex/eventfd), the mailbox write +
read, and the loop's selector seeing the mailbox fd.

## 3. Limitations of the prototype (what inline mode can't do yet)

* **Unix only**, like the default path (`add_reader` on sockets; Windows' Proactor loop has
  no `add_reader`).
* **The GIL is held during protocol work.** Default mode does socket I/O and row copying
  without the GIL on the tokio thread; inline mode does it inside the Python callback. For a
  single-loop process that's free; for processes with busy Python threads it is a few tens
  of µs of GIL per query they can't use.
* **One loop thread.** The connection is bound to the thread that created it (true before
  too); a `pump` from another thread (GC) is turned into a wake-up byte instead.
* **COPY backpressure still needs the tokio runtime context** for its 50 ms re-check timer
  (`bulk::wait_for_socket`); inline mode enters the runtime for that, so the tokio thread
  still exists (idle) in inline-only processes.
* **No `sslmode=require`** (unchanged), no server-side cancel (unchanged).
* **`connect()` resolves names with `loop.getaddrinfo`**, i.e. asyncio's default executor
  thread for non-numeric hosts (asyncio's own behaviour; `127.0.0.1` needs no thread).
* Pipelining, streaming of large results, transactions, bulk writes and disconnect handling
  all work (tested) but were not tuned.

## 4. Measurements (host, macOS, Docker Desktop; relative numbers only)

Machine shared with other work; PostgreSQL in Docker Desktop (the ~300 µs VM round trip
dominates wall latency, and every socket syscall here goes through Docker's userspace port
forward, which inflates kernel CPU attributed to the client). Loop: uvloop unless stated.
Query: the 10-column `people` point lookup into a Struct (`fastpg-struct`, asyncpg's
`fetchrow`). Script: `bench/cpu_per_request.py` (the headline tables below come from it;
the earlier, noisier matrix came from an ad-hoc script with the same columns).

```
FASTPG_DSN=postgresql://fastpg:fastpg@127.0.0.1:55432/fastpg_lat_c
uv run yeet ./bench/cpu_per_request.py -n 5000                         # fastpg-struct spin 200, fastpg-inline, asyncpg-struct
FASTPG_SPIN_US=0 uv run yeet ./bench/cpu_per_request.py -n 5000 -d fastpg-struct
uv run yeet ./bench/cpu_per_request.py -n 20000 -t 16 -d 'fastpg-struct' -d fastpg-inline
FASTPG_SPIN_US=0 uv run yeet ./bench/cpu_per_request.py -n 20000 -t 16 -d fastpg-struct
uv run yeet ./bench/cpu_per_request.py -n 400 -i 0.01                  # ~100 req/s sporadic
uv run yeet ./bench/cpu_per_request.py -n 5000 -l asyncio
```

**Headline: sequential point lookups, 3,000 requests, three runs (quiet machine), process
CPU per request in µs, median (all three):**

| driver | process CPU/req | loop-thread CPU/req | p50 wall µs | p99 wall µs |
|---|---:|---:|---:|---:|
| fastpg, spin 200 (default) | **84.3** (84.3, 81.9, 89.0) | 9–12 | 296–349 | 461–743 |
| fastpg, spin 0 | **53.8** (53.6, 53.8, 61.9) | 10–14 | 333–379 | 477–547 |
| **fastpg-inline** | **39.7** (38.9, 39.7, 52.8) | = total | 310–361 | 461–540 |
| asyncpg | **41.7** (41.4, 41.7, 42.5) | = total | 273–326 | 455–492 |

Inline mode gets fastpg's total CPU per request to asyncpg's level (39.7 vs 41.7 µs), 26%
below the spin-0 default and 53% below the spin-200 default, while doing the protocol and
decoding in Rust. Wall latency is equal within noise (the Docker VM round trip dominates
p50 here). An earlier matrix on a busier machine gave the same ordering with wider spreads
(inline 43–87, spin 0 58–75, spin 200 83–93, asyncpg 94–118) and the same on the stdlib
asyncio loop (inline 98–100, spin 0 59–68, spin 200 88–105, asyncpg 140–156; the stdlib
loop's per-callback overhead hits the two single-thread drivers hardest).

Why inline's loop-thread CPU is ~40 µs when the default path's loop thread spends ~10:
inline moves the socket syscalls onto the loop thread, and on this host they are the
dominant cost (asyncpg, which also does them on the loop thread, lands at the same number).
The decode work itself is ~0.5 µs per row (`perf-notes.md`). On Linux, where a loopback
`write`/`read` is ~1–2 µs, the expectation is loop ≈ 10 µs + syscalls ≈ 5 µs + xitca
parsing ≈ 2 µs + callback dispatch ≈ 3 µs, i.e. roughly 20–25 µs total per request against
the 51 µs (spin 0) / 69–107 µs (spin 200) measured on Linux for the default path and
29–44 µs for asyncpg. That is a prediction, not a measurement; the Linux runs decide.

**16 tasks on one connection (pipelined), 16,000–20,000 requests, three runs (stable):**

| driver | throughput ops/s | process CPU/req µs | CPU % |
|---|---:|---:|---:|
| fastpg, spin 200 | 26.3k, 27.8k, 28.2k | 36.7, 35.9, 35.5 | 97–100 |
| fastpg, spin 0 | 26.0k, 27.0k, 27.9k | 37.0, 36.0, 35.6 | 96–100 |
| **fastpg-inline** | **28.1k, 29.5k, 29.1k** | **29.0, 28.5, 28.6** | 81–84 |

With the hop cost amortised over pipelined requests the remaining difference is the
mailbox/wake-up machinery: inline saves ~7 µs per request (20%) and finishes more work on
82% of a core instead of 100%. (asyncpg does not pipeline one connection: with 16 tasks on
its pool of size 1 it serialises at 1.3k ops/s and 119 µs CPU/req; not comparable.)

**Sporadic traffic (~100 req/s, `asyncio.sleep(0.01)` between requests), process CPU/req:**
fastpg spin 200: 565 µs; spin 0: 637 µs; inline: 235 µs; asyncpg: 473 µs. These include the
timer wake-up of the sleeping loop (and macOS idle-wake costs), so only the ordering is
meaningful: inline wakes one thread per request, the default path wakes two.

**Idle connection, 2 s:** 0.26 ms (default) vs 0.13 ms (inline) of process CPU. Neither
spins; the spin window only follows activity.

Smoke runs with a 3-column tuple query showed the same shape (inline loop CPU ~39 µs vs
default loop ~10 µs, equal wall latency).

## 5. Inline mode on one CPU or less (k8s 500m, Lambda)

Under a CFS quota the loop and the tokio thread share one core: every hop is a same-core
context switch plus a wake-up, and the quota is charged for both threads. Spinning is
actively harmful there: the spinning tokio thread takes the only core away from the loop
thread (Linux data: at `--cpus=0.25`, spin 200 is slower than spin 0, 1.8k vs 2.8k
ops/s, asyncpg 7.1k). Inline mode has one thread, no spinning, no hop, and its CPU per
request is the sum of work that has to happen anyway; it is the shape asyncpg has, with
Rust doing the protocol and decoding. It therefore cannot be worse than the default path
on a single core and should be close to or below asyncpg once the hop and mailbox costs
are gone (sections 4 and 7).

Could it be auto-selected? `std::thread::available_parallelism()` returns 1 for
`--cpuset-cpus=0` and for `--cpus=1/0.5/0.25` (Rust rounds cgroup v1/v2 quotas up and
intersects with the affinity mask), so `inline = available_parallelism() < 2` would pick
inline mode in exactly those containers. Caveats: AWS Lambda reports the sandbox's vCPUs,
not the memory-proportional share (a 1 GB function may still see 2); and Docker's `--cpus`
sets a quota, which `available_parallelism` honours, while a Kubernetes *request* without a
limit sets no quota at all. An explicit `FASTPG_IO=inline|thread` override would be needed
next to the heuristic. Given the decision below, this is moot: loop-native becomes the
only path and the question disappears.

## 6. Trade-offs and failure modes

* **Latency of the first query after idle**: inline needs one wake-up (the loop's own),
  the default path two; this is where inline wins most.
* **Big result sets**: decoding and reading interleave per pump (xitca reads what the
  kernel has buffered, the pump decodes the chunks, the kernel keeps receiving). There is
  no longer a second thread doing the memcpy while the loop decodes, so the 10k-row case
  loses some overlap; it still streams in 1024-row chunks, so memory is unchanged. Not
  measured here.
* **Level-triggered readiness**: if the driver ever stopped reading while the socket stayed
  readable, asyncio would call `on_readable` every iteration (a busy loop). xitca only stops
  reading after EOF or a fatal error, both of which make `EofWatch` signal EOF, which ends
  the driver, which marks the connection dead, which detaches the reader in the same pump.
  The disconnect tests exercise this.
* **fd reuse**: Rust never closes the fd and Python closes it only from `detach`, which runs
  after the driver is gone, so no Rust read can hit a recycled descriptor. A connection that
  is never closed and never dies keeps its hooks (and the socket) until `__del__` closes
  it; that is the pre-existing `ResourceWarning` path.
* **Off-thread wakes** (COPY timer, foreign-thread GC) reach the loop through the mailbox
  byte; they are correct but take one extra loop iteration.
* **GIL contention**: see limitations. If it matters, the pump could release the GIL around
  `Executor::run` (nothing in a poll touches Python), at the cost of two GIL transitions
  per pump.
* **Thread-affinity bugs**: all state is `Mutex`-protected (the `Connection` pyclass is
  `frozen`), so misuse from another thread cannot corrupt memory; `pump` refuses to run off
  the loop thread.

## 7. Migration plan: loop-native as the only path

Decision: all connection I/O moves to the loop thread; no tokio thread
on query paths; `spin` and the cross-thread mailbox are deleted. Route 1 (this prototype)
is the base. Steps, in order, each leaving the suite green:

1. **Make inline the only mode.** `Connection::new` always builds the executor;
   `connect()` in Python always goes through `_open_socket` + `attach` (rename it
   `connect`); delete `Connection::connect` (tokio) and `net::connect` (tokio lookup +
   `TcpStream::connect`), keep `net::config`, `net::targets`, `net::handshake`, `EofWatch`,
   `IoWatch`. Delete `Shared::inline: Option<_>` branching, `Shared::touch`,
   `runtime::spin`, and `runtime.rs` entirely. Drop the `rt-multi-thread`, `time`, `net`
   tokio features from `native/Cargo.toml` (keep `sync` and `macros` for `RwLock`, `watch`,
   `Notify`, `select!`; or replace `select!` with a small `poll_fn`); xitca keeps pulling
   tokio `rt`+`sync` as a compile-time dependency, nothing starts a runtime.
2. **Delete the mailbox.** `Shared::post` becomes a push onto `Mutex<Vec<Completion>>`;
   `drain` takes it. The only off-thread producer left is a foreign-thread `__del__`:
   handle it in Python (`Connection.__del__` → `loop.call_soon_threadsafe(native.close)`),
   so `mailbox.rs`, `Poker`, `fileno()` and the second `add_reader` go away. `Executor`'s
   wake path then asserts the loop thread (or, for safety, keeps a `call_soon_threadsafe`
   callable to fall back on).
3. **Bulk writes.** `execute_many`/`insert_many`/`copy_in` already run as executor tasks.
   Remove the `tokio::time::timeout` from `bulk::wait_for_socket`: wait on
   `IoWatch::progress.notified()` alone; the 50 ms re-check existed to notice a dead
   connection, which `DeadWatch`/`unless_dead` already handle, and `signal_eof` also
   notifies `progress`. `Bulk::{feed, finish, abort}` keep pumping. No timers remain.
4. **Streaming of large results.** Unchanged mechanics (1024-row chunk posts, decoded in
   the pump that received them). Optionally make the executor budget-aware: after N
   milliseconds of a pump, return to the loop (`loop.call_soon(native.drain)`) so one huge
   result doesn't starve other callbacks. Measure the 1k/10k-row cases on Linux first.
5. **Pool, transactions, savepoints.** Pure Python over `Connection`/`Cursor`
   (`_pool.py`, `Transaction`), no change beyond the removed `inline=` parameter. Pool
   connections stay bound to the loop as before.
6. **Server disconnect ([ADR 0002](../adr/0002-protocol-on-the-event-loop.md)).** `EofWatch` wraps `LoopSocket` exactly
   as it wrapped tokio's stream (0-byte read or fatal read/write error → `eof` → `serve`
   drops the driver → `dead` → every pending operation fails → `detach`). All three
   disconnect tests pass inline today; keep them.
7. **Statement cache, decoding, params, errors**: unchanged (`stmt_cache.rs`, `decode.rs`,
   `structs.rs`, `rows.rs`, `params.rs`, `errors.rs`, `msgpack.rs`, `json.rs`).
8. **Docs**: README architecture diagram and limitations (`Not fork-safe once used` goes
   away: no threads), the ADRs, `perf-notes.md` numbers redone on
   Linux.

What is deleted: `runtime.rs` (runtime + spin), `mailbox.rs`, `Connection::connect`,
`net::connect`, `Shared::{spawn tokio branch, touch}`, `FASTPG_IO_THREADS`,
`FASTPG_SPIN_US`. What is new: `event_loop.rs` (~330 lines), `Shared::pump`, the Python socket
connect. Net code size goes down.

### Risks

* **Linux numbers are unmeasured.** The macOS host can't resolve sequential inline vs
  spin 0; the prediction in section 4 could be wrong if asyncio's callback dispatch or
  xitca's boxed `ready()` futures cost more than expected on Linux. Mitigation: the Linux
  container matrix, `bench/cpu_per_request.py`, before step 1.
* **Loss of the second core** for the 1k/10k-row fetches on multi-core hosts (the memcpy
  overlap). Possibly a regression on big results where fastpg currently beats asyncpg by
  streaming; mitigation: measure; if needed, keep decode chunking and consider releasing the
  GIL during `run()`.
* **GIL held during I/O** for apps with other Python threads (section 3).
* **Level-triggered spin** if a future xitca version changes when it stops reading;
  mitigation: a debug assertion/counter for "on_readable without a read", and the detach
  path.
* **Off-thread use**: a connection used from a thread that is not its loop's is already
  unsupported; with the mailbox gone, the GC/`__del__` path must go through
  `call_soon_threadsafe` or it is lost. Covered by `test_unclosed_connection_warns_and_cleans_up`.
* **xitca upgrade risk**: `AsyncIo` is a public but "temporary"-commented trait
  (`poll_ready` doc); a future version might change the seam. Both `connect_io` and
  `AsyncIo` are public API today.
* **Windows** stays unsupported (as now).
* **Cancellation semantics** are unchanged (a cancelled awaiter leaves the server-side query
  running until it completes; no server-side cancel). Not a regression.

## 8. How to reproduce

```
uv sync --all-groups --all-extras --reinstall-package fastpg
FASTPG_DSN=postgresql://fastpg:fastpg@127.0.0.1:55432/fastpg_lat_c uv run pytest -q tests/test_loop_io.py
FASTPG_DSN=... uv run pytest -q                                   # whole suite, 373 tests
PYO3_PYTHON=$(uv python find) cargo test --manifest-path native/Cargo.toml   # 12 tests, 2 for the executor
uv run yeet ./bench/cpu_per_request.py ...                        # section 4
uv run yeet ./bench/run.py -d fastpg-struct -d fastpg-inline -d asyncpg-struct   # standard suite
```
