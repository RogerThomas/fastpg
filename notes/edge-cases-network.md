# Edge cases: slow, stalled and failing servers and networks

What fastpg does when the network is slow, stalls, drops connections or fragments bytes,
and when the server restarts, dies or is down; what was fixed; what is still open, with a
proposed design for each. Every behaviour below is pinned by a test in
`tests/test_network_*.py`. Measured on macOS (Docker Desktop, PostgreSQL 18.6).

## Tools

* **`FaultProxy`** (`tests/faults.py`): a TCP forwarder on 127.0.0.1. Per direction
  (`to_server`, `to_client`): `delay` (latency added to every read, without limiting
  throughput: reads and writes go through a queue of `(due, bytes)`), `fragment` (at most
  N bytes per write, each sent on its own), `stall()` / `resume()` (bytes are held, not
  dropped), `cut_after(n, rst=...)` (drop every connection after exactly n more bytes).
  Proxy-wide: `drop(rst=...)` (FIN, or RST via `SO_LINGER` 0), `refuse` (reset new
  connections at once), and counters (`accepted`, `forwarded`, `ended`: connections whose
  sender closed, e.g. `to_server.ended` counts sockets fastpg closed).
* **`DisposableServer`** (`tests/faults.py`): a PostgreSQL container of the tests' own
  (`fastpg-edge-net-<random>`, on a fixed host port so `docker restart` keeps the
  address), with `pause` / `unpause` / `kill` / `start` / `restart` / `heal`. It is never
  the shared test server or a user's database.
* conftest fixtures: `fault_proxy` + `proxied_dsn` (to the test server, `sslmode=disable`),
  `tls_fault_proxy` + `tls_proxied_dsn` (to the TLS-only server, `sslmode=require`),
  `edge_server` (session-scoped container) and `edge` (healed before each test).

A finding about the tools themselves: **macOS drops an RST that arrives while the
receiver's window is closed** (its receive buffer full). The receiver's socket then stays
`ESTABLISHED` forever: a plain asyncio client hung in 26 of 30 tries when a server reset
the connection mid-stream. The proxy therefore lets the receiver drain for 100 ms before
an RST. In production this is the "lost RST" case below (half-open connection).

## Summary

| # | scenario | behaviour | bug? | fixed? | tests |
|---|---|---|---|---|---|
| 1 | 50-200 ms RTT | correct results; a cached statement costs 1 RTT; 200 pipelined statements on one connection take about 1 RTT; a pool pipelines while it grows; bulk writes take a few RTTs per chunk; 1 s RTT works (no internal timeouts) | no | - | `test_network_latency.py` |
| 2 | server stops responding (stall, `docker pause`) | `asyncio.timeout` around a query returns on time; the connection stays usable and gets its own result after the stall; a pool recovers | yes: 4 | yes | `test_network_stall.py` |
| 2a | | `close()` cancelled by a timeout cancelled the shared closed future: every later `close()` raised `CancelledError` | yes | yes (c83fb3f) | `test_close_completes_after_an_earlier_close_timed_out` |
| 2b | | a timeout around `conn.transaction()`, `pool.transaction()` or `pool.acquire()` waited for the ROLLBACK / session reset behind the stalled statement: forever on a stalled server, as long as the statement on a slow one | yes | yes (027ed5b) | `test_timeout_bounds_a_*` |
| 2c | | `pool.close()` hung forever on a connect in progress to a server that doesn't answer the startup | yes | yes (199e95e) | `test_pool_close_does_not_wait_for_a_stalled_connect` |
| 2d | | a cancelled `connect()` left its socket open and registered until the server answered (maybe never) | yes | yes (5b8de22) | `test_cancelled_connect_to_a_stalled_server_closes_its_socket` |
| 2e | | `close()` (and so `async with` exit, `pool.close()`) waits for the server's EOF after Terminate: forever on a stalled or partitioned server | yes | **open** (design below) | `test_close_completes_after_...` (documents it) |
| 3 | RST / FIN mid `select_many`, COPY, `execute_many`, transaction | fails at once with `ConnectionClosedError`; connection closed; nothing of the write committed; a pool replaces the connection | message only | yes (306de25): it said "connection is closed", as after the caller's own `close()`; now "connection lost: closed by the server or the network" | `test_network_drop.py` |
| 4 | `docker restart` / `kill` / `stop`, `pg_terminate_backend` | statements in flight fail at once (57P01 `OperationalError`, or `ConnectionClosedError` after a kill); a pool recovers by itself, 0.27 s after `docker restart` began, 0.1 s after `docker start` returned; terminating every idle session at once is absorbed | no | - | `test_network_server.py`, plus existing `test_pool.py` / `test_connection.py` / `test_tls.py` terminate tests |
| 5 | database down / refusing / unreachable | refused: every statement fails at once with its own connect attempt (no backoff); unreachable (SYNs dropped): connect waited for the OS (75 s on macOS); accepts TCP but never answers the startup: waited forever | yes | `connect_timeout` added (69c3bda); unknown DSN keys now named (7e8214c); backoff **open** | `test_network_connect.py` |
| 6 | `pg_sleep`, lock waits, `statement_timeout` | timeout returns on time; the statement runs on server-side and its connection stays busy; a cancelled UPDATE waiting on a lock still commits when the lock is released; `statement_timeout` from the DSN raises `QueryCanceledError` and the pool keeps the connection; the pool routes around a busy connection | no (known: no server-side cancel) | cancel **open** | `test_network_slow.py` |
| 7 | 1- and 7-byte fragments (plain, and TLS records byte by byte) | correct: startup, rows across chunks, structs, 20 kB values, errors, pipelining, `execute_many`, COPY, transactions | no | - | `test_responses_in_tiny_fragments`, `test_tls_records_in_one_byte_fragments` |
| 8 | TLS variants | stall + recovery, 1-byte records, pool replacing a terminated busy TLS session; cut without close_notify and terminate were already covered by `test_tls.py` | no | - | `test_stalled_tls_connection_recovers`, `test_pool_replaces_a_terminated_busy_tls_connection` |

## 1. High latency

Through the proxy with 25-100 ms each way (50-200 ms RTT):

* A cached statement costs one round trip; the first use of a statement two (prepare,
  then bind/execute). `select_many` of 5,000 rows, `execute` and errors are unaffected.
* 200 concurrent `select_one`s on one connection complete in about one RTT (pipelined).
* A pool (`min_size=1, max_size=4`) with 400 concurrent statements: correct results;
  at most `4 x 8` statements are in flight, so it takes about 13 RTTs plus growth (each new
  connection costs several RTTs of startup, while the statements pipeline on the busy
  connections). Measured 1.6-2 s at 100 ms RTT.
* `pool.transaction()`, `copy_in` (30,000 rows), `execute_many` and `insert_many`
  (5,000 rows each): correct counts in about 2 s at 100 ms RTT: a chunk at a time with two
  in flight, not a round trip per row.
* A 1 s RTT: `create_pool`, a statement and a transaction take about 10 s in all, and
  nothing gives up: fastpg has no internal timeouts.

## 2. Server stops responding

Two ways: the proxy stalls (the TCP connections stay up, nothing is forwarded), and
`docker pause` (Docker's port forwarder keeps accepting; the server answers nothing).

What works:

* `asyncio.timeout(...)` around a query raises `TimeoutError` on time. The connection is
  not marked dead (nothing tells it the server is gone); once the server answers again,
  the abandoned statement's result is discarded and the next statement gets its own
  result.
* In a pool, an abandoned shared statement keeps its slot until the server is past it
  (fastpg sends a no-op behind it and frees the slot when that returns); other statements
  are routed elsewhere, and the pool opens a connection meanwhile. Once the server answers
  again, slots are freed and the pool is back to normal (`in_flight == 0`).

Fixed:

* **2a: retrying a timed-out `close()` raised `CancelledError`.** `close()` awaited the
  connection's `closed` future directly, so cancelling the awaiting task cancelled that
  shared future. It is now awaited through `asyncio.shield`.
* **2b: a timeout didn't bound a transaction or an acquired connection.** On the way out of
  a cancelled block, `Transaction.__aexit__` awaited ROLLBACK and the pool awaited its
  session reset, both queued behind the statement the caller was waiting for. With a
  server that stopped answering, `asyncio.timeout(1)` never returned; with
  `pg_sleep(10)`, it returned after 10 s. Now, when the running task is being cancelled
  (`Task.cancelling()`), the rollback is sent without being awaited (the connection runs it
  before anything sent later, so a `Connection` user's next statement runs outside the
  transaction), and the pool releases the connection in the background: it stays out of
  rotation until its reset is done, then rejoins (or is discarded if the reset fails).
* **2c: `pool.close()` hung on a connect in progress.** It waited for every background task,
  including a growth `connect()` to a server that never answers the startup. The pool now
  keeps opening connects as tasks and cancels them when it closes.
* **2d: a cancelled `connect()` leaked its socket.** `close()` during the handshake only set
  a flag; the handshake task kept the socket open and registered with the loop until the
  server answered. `close()` now notifies the handshake task, which abandons the handshake,
  and the socket is detached and closed right away.

Open:

* **2e: `close()` waits for the server's EOF.** After the last statement, xitca's driver
  writes Terminate and then waits until it reads EOF. A healthy server closes at once; a
  stalled one, or one behind a partition or a lost RST (below), never does. So `close()`,
  the exit of `async with await fastpg.connect(...)` and `pool.close()` hang. Wrapping
  `close()` in `asyncio.timeout` works now (2a), but the socket and driver task stay until
  the server answers. Proposed:
  1. a native `abort()`: notify the connection's `IoWatch.eof` (the `serve` task then
     drops the driver, marks the connection dead, fails pending operations with
     `ConnectionClosedError` and detaches the socket). Exposed as `Connection.terminate()`
     (asyncpg's name) and used by the pool for connections still busy after a
     `pool.close(timeout=...)` grace period; and
  2. don't wait for EOF after Terminate: follow the frontend messages written by
     `EofWatch` (as `Framer` does for reads; after startup every message is tagged), and
     once a complete `X` (Terminate) has been written and flushed, end `serve` as on EOF.
  Both are API or behaviour decisions, so not done here.

## 3. Connection reset or closed mid-result

RST and FIN, in the middle of a 2.3 MB streamed `select_many` (after 1 MB), a 5.5 MB
`copy_in`, a 20,000-row `execute_many`, a pipelined batch of `pg_sleep`s, and a pool
transaction:

* The call fails at once with `ConnectionClosedError`; `conn.closed` is true; later calls
  fail at once too. No hang, no partial result, no decode error.
* Nothing of an interrupted write is committed (the session dies, the server rolls back).
* A pool drops the connection and the next statement runs on a new one
  (`size`, `idle`, `in_flight` back to `1, 1, 0`; the proxy saw exactly one new connection).

Fixed: the message was "connection is closed", the same as after the caller's own
`close()`. A connection ended by the server or the network now fails with "connection
lost: closed by the server or the network" (still `ConnectionClosedError`); "connection is
closed" means `close()` was called.

Open (half-open connections): **a lost RST or a network partition is never noticed.** If
neither FIN nor RST arrives (a partition, a crashed host, macOS dropping an RST into a
closed window as above), the socket stays `ESTABLISHED`: queries wait until the caller's
timeout, and `close()` hangs (2e). fastpg sets no `SO_KEEPALIVE`, while libpq enables
keepalives by default. Proposed: libpq's keys, `keepalives` (default 1),
`keepalives_idle`, `keepalives_interval`, `keepalives_count`, applied in
`_connect_candidate` (`SO_KEEPALIVE`, `TCP_KEEPIDLE` / macOS `TCP_KEEPALIVE`,
`TCP_KEEPINTVL`, `TCP_KEEPCNT`), split off the DSN like `connect_timeout`; plus
`tcp_user_timeout` on Linux (`TCP_USER_TIMEOUT`) to bound unacknowledged writes. A keepalive
or write timeout surfaces as a fatal socket error, which `EofWatch` already turns into
`ConnectionClosedError` for everything pending.

## 4. Server restart and `pg_terminate_backend`

On the tests' own container:

| event | statements in flight | the `Connection` | the pool |
|---|---|---|---|
| `docker restart` (fast shutdown) | 57P01 `OperationalError` "terminating connection due to administrator command" in 0.03 s | closed; later calls "connection lost" | first success 0.27 s after the restart began (one "server closed the connection during SSL negotiation" meanwhile); back to `min_size` |
| `docker kill` (SIGKILL) | `ConnectionClosedError` "connection lost" in 0.04 s | closed | while down: each statement fails with its connect (refused, reset during TLS negotiation, or, right after the kill, Docker's forwarder accepting and not answering: once 15 s before the connect failed, so the test uses `connect_timeout=2`); after `docker start`: first success 0.1 s after it returned (one 57P03 "the database system is starting up" meanwhile) |
| `docker stop` | 57P01 | closed | as for kill |
| `pg_terminate_backend` of every idle session (3) | - | - | the dead connections are discarded when picked; 6 concurrent statements succeed; back to 3 |

Already covered elsewhere: a terminated idle or busy connection (`test_connection.py`),
a terminated pool connection idle, busy or pipelined (`test_pool.py`), terminate and a cut
without close_notify over TLS (`test_tls.py`).

Errors seen while a server is coming back are `InterfaceError` (connect failures) or
`OperationalError` (57P03); the TLS-negotiation ones ("SSL negotiation failed: Connection
reset by peer (os error 54)") lack the "could not connect to server:" prefix that the
socket-level ones have. Cosmetic; left as is.

## 5. The database is down, refusing or unreachable

Before:

* **Refused** (nothing listening): `connect()` and `create_pool()` fail at once
  (`InterfaceError: could not connect to server: [Errno 61] ... Connection refused`).
* **Unreachable** (SYNs unanswered: an unrouted or firewalled address): `connect()`, and a
  pool's first statement, waited for the OS's TCP connect timeout: 75 s on macOS (Linux:
  about 130 s with the default `tcp_syn_retries=6`).
* **Accepts TCP, never answers the startup** (paused or overloaded server, Docker's
  forwarder to a dead container, a middlebox): waited forever. `pool.close()` then hung
  too (2c), and a cancelled connect leaked its socket (2d).
* An unknown DSN key failed with `InterfaceError('WIP error type with thread backtrace:
  disabled backtrace')` (xitca's parse errors carry no detail). Users trying libpq's
  `connect_timeout` hit exactly this.

Implemented:

* **`connect_timeout`**, libpq's semantics: whole seconds, 0 / negative / absent wait
  indefinitely, 1 means 2 (libpq's minimum), not an integer is an error worded like
  libpq's. It applies to each address tried, from its TCP connect to the end of the
  startup (TLS, authentication). On expiry: `InterfaceError("could not connect to server:
  connect to ('192.0.2.1', 5432) failed: timeout expired (connect_timeout)")` or
  `"... startup with ('127.0.0.1', 5432) failed: timeout expired (connect_timeout)"`, and the
  socket is closed. It applies to `create_pool` and to pool growth through the DSN; a
  pool with nothing open fails its waiting statements with that error. Parsed in Rust
  (`tls::split_dsn` takes it off the DSN with the TLS keys; `dsn_connect_timeout`), applied
  in Python (`asyncio.timeout_at` around each candidate's connect and the handshake).
* **Unknown DSN keys are named**: `unsupported connection option "keepalives"`; any other
  xitca parse failure says `invalid connection string: malformed, or an invalid port or
  target_session_attrs value`.

Open: **no backoff.** With nothing open (all connections dead, or `min_size=0`), every
statement starts a connect and fails with it: against a refusing database, 20 statements
made 40 connect attempts in under 2 s, i.e. a reconnect storm at the request rate when
the database comes back. Two per statement: `_route` grows the pool for a statement that
must wait, and `_wait`'s `_dispatch` grows it again. Proposed:

1. grow once per waiting statement: `_route` doesn't call `_grow()` when it returns None
   (the waiting statement's dispatch does). This changes how fast a saturated pool grows
   (one connection per waiting statement instead of two), which `notes/pool.md` measured,
   so it should be re-benchmarked rather than changed here;
2. after a failed connect, no new connect for `min(0.1 s * 2^failures, 10 s)` (with
   jitter); meanwhile, a statement that finds no usable connection fails at once with the
   last connect error, and `_tidy` retries when the backoff ends; a success resets it.
   With `connect_timeout`, an unreachable database then costs one bounded wait per backoff
   step instead of one per statement.

When the database refuses new connections but some are open, growth failures are dropped
quietly and the open connections keep serving (pipelined).

## 6. Slow queries

* `asyncio.timeout(0.2)` around `pg_sleep(1)` raises on time; the next statement on that
  connection completes about 0.8 s later, when the sleep ends (no server-side cancel).
* A cancelled `UPDATE` waiting for a row lock held by another session still runs, and
  commits, when the lock is released. A caller's timeout is not a cancel of the write.
* `options=-c statement_timeout=100` in the DSN: `pg_sleep(2)` raises
  `QueryCanceledError` (57014) after 0.1 s; the connection stays usable, and a pool keeps it
  (57014 isn't session-fatal).
* In a pool of two, a cancelled `pg_sleep(1)` keeps its slot, and 20 sequential fast
  statements meanwhile take under 0.5 s in all (routed to the other connection).

Open: **server-side cancel.** Proposed design:

* Capture the BackendKeyData (`K`: process id and secret key) as the startup response
  passes through `EofWatch`'s `Framer` (xitca keeps its own copy private, and its
  `Session::query_cancel` needs a tokio runtime), and keep the address the socket
  connected to.
* `cancel()`: open a new socket to that address (bounded by `connect_timeout`), write the
  16-byte CancelRequest (length 16, code 80877102, pid, key), wait for the server to close
  it. Plaintext works with every server version; PostgreSQL 17+ also accepts it over TLS,
  which `sslmode=require`+ should use (the TLS code path is reusable).
* Use it only where it cancels the right statement: when the abandoned statement is the
  only one in flight on its connection (always for `acquire()` / `transaction()`, a
  `Connection` with one statement, a shared pool slot whose connection has load 1). With
  pipelining, the server cancels whatever runs, possibly another caller's statement.
* The race: a cancel that arrives after its statement finished hits the next one. The
  postmaster signals the backend before it closes the cancel socket, and an idle backend
  ignores the signal; so don't send anything on that connection until the cancel socket
  has closed. The pool already holds the slot until the server is past the statement
  (`_flush_ghosts`); it would start the cancel there and send its no-op after the cancel
  socket closed.

## 7. Fragmented delivery

Every byte from the server in its own TCP write (and every byte to it), and 7-byte
fragments: the startup and authentication, rows spanning chunk boundaries (600 rows,
two streamed chunks), Structs, a 20 kB value, an error response, 20 pipelined statements,
`execute_many`, COPY and a transaction are all correct. Over TLS with 1-byte fragments both
ways, rustls reassembles the records. No bug.

## 8. TLS

Stall and recovery over TLS (`sslmode=require` through the proxy), 1-byte TLS records,
and a pool replacing a terminated busy TLS session behave as without TLS.
`test_tls.py` already covered a terminated connection and a cut without close_notify.

## Running these tests

`uv run pytest -q tests/test_network_*.py`: 53 tests, about 55 s (the slowest, a 1 s RTT
startup, takes 10 s; the long ones are marked `@pytest.mark.timeout(60)`). The destructive
ones start the tests' own container (`fastpg-edge-net-*`) on first use, removed at the end
of the session, also when `FASTPG_DSN` is set.
