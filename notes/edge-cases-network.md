# Edge cases: slow, stalled and failing servers and networks

What fastpg does when the network is slow, stalls, drops connections or fragments bytes,
and when the server restarts, dies or is down; what was fixed; what is still open, with a
proposed design for each. Every behaviour below is pinned by a test in
`tests/test_network_*.py`. Measured on macOS (Docker Desktop, PostgreSQL 18.6).

## Tools

* **`FaultProxy`** (`tests/faults.py`): a TCP forwarder on 127.0.0.1 (or, with
  `start_unix(path)`, from a unix socket). Per direction
  (`to_server`, `to_client`): `delay` (latency added to every read, without limiting
  throughput: reads and writes go through a queue of `(due, bytes)`), `fragment` (at most
  N bytes per write, each sent on its own), `stall()` / `resume()` (bytes are held, not
  dropped), `cut_after(n, rst=...)` (drop every connection after exactly n more bytes).
  Proxy-wide: `drop(rst=...)` (FIN, or RST via `SO_LINGER` 0), `refuse` (reset new
  connections at once), `unreachable()` (new connections hang: the port keeps listening,
  nothing accepts, and its full queue makes the kernel drop new SYNs), and counters
  (`accepted`, `forwarded`, `ended`: connections whose sender closed, e.g.
  `to_server.ended` counts sockets fastpg closed).
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
| 2a | | `close()` cancelled by a timeout cancelled the shared closed future: every later `close()` raised `CancelledError` | yes | yes (c83fb3f) | `test_a_timed_out_close_terminates_the_connection` |
| 2b | | a timeout around `conn.transaction()`, `pool.transaction()` or `pool.acquire()` waited for the ROLLBACK / session reset behind the stalled statement: forever on a stalled server, as long as the statement on a slow one | yes | yes (027ed5b) | `test_timeout_bounds_a_*` |
| 2c | | `pool.close()` hung forever on a connect in progress to a server that doesn't answer the startup | yes | yes (199e95e) | `test_pool_close_does_not_wait_for_a_stalled_connect` |
| 2d | | a cancelled `connect()` left its socket open and registered until the server answered (maybe never) | yes | yes (5b8de22) | `test_cancelled_connect_to_a_stalled_server_closes_its_socket` |
| 2e | | `close()` (and so `async with` exit, `pool.close()`) waited for the server's EOF after Terminate: forever on a stalled or partitioned server; a cancelled close left the socket open | yes | yes (7312aee, 84d75e2) | `test_*_returns_promptly_from_a_stalled_server`, `test_a_timed_out_close_terminates_the_connection`, `test_cancelled_pool_close_terminates_a_checked_out_connection` |
| 3 | RST / FIN mid `select_many`, COPY, `execute_many`, transaction | fails at once with `ConnectionClosedError`; connection closed; nothing of the write committed; a pool replaces the connection | message only | yes (306de25): it said "connection is closed", as after the caller's own `close()`; now "connection lost: closed by the server or the network" | `test_network_drop.py` |
| 3a | lost RST, partition (half-open) | never noticed: no TCP keepalives | yes | yes (edb7548): libpq's keepalive keys and `tcp_user_timeout`, keepalives on by default | `test_network_keepalives.py` (keys only) |
| 4 | `docker restart` / `kill` / `stop`, `pg_terminate_backend` | statements in flight fail at once (57P01 `OperationalError`, or `ConnectionClosedError` after a kill); a pool recovers by itself, 0.27 s after `docker restart` began, 0.1 s after `docker start` returned; terminating every idle session at once is absorbed | no | - | `test_network_server.py`, plus existing `test_pool.py` / `test_connection.py` / `test_tls.py` terminate tests |
| 5 | database down / refusing / unreachable | refused: every statement fails at once with its own connect attempt (no backoff); unreachable (SYNs dropped): connect waited for the OS (75 s on macOS); accepts TCP but never answers the startup: waited forever | yes | `connect_timeout` added (69c3bda); unknown DSN keys now named (7e8214c); one connect per waiting statement (bac365b); backoff **open** | `test_network_connect.py` |
| 6 | `pg_sleep`, lock waits, `statement_timeout` | timeout returns on time; the statement ran on server-side and its connection stayed busy, so a cancelled UPDATE waiting on a lock still committed when the lock was released; now cancelled on the server when alone on its connection; `statement_timeout` from the DSN raises `QueryCanceledError` and the pool keeps the connection; the pool routes around a busy connection | yes (no server-side cancel) | yes (85a86ed, 90f0707): when alone on its connection | `test_network_slow.py` |
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
  (fastpg sends a no-op behind it and frees the slot when that returns; alone on its
  connection, it is also cancelled there, see 6); other statements are routed elsewhere,
  and the pool opens a connection meanwhile. Once the server answers again, slots are freed
  and the pool is back to normal (`in_flight == 0`).

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
* **2e: `close()` waited for the server's EOF.** After the last statement, xitca's driver
  writes Terminate and then waits until it reads EOF. A healthy server closes at once; a
  stalled one, or one behind a partition or a lost RST (below), never does. So `close()`,
  the exit of `async with await fastpg.connect(...)` and `pool.close()` hung, and a
  cancelled `close()` left the socket and driver task open. Now, as libpq's `PQfinish`
  and pgx's `Close` do, the close doesn't wait:
  1. `EofWatch` follows the frontend messages written after the startup (every one is
     tagged). Once a complete `X` (Terminate) has been written and flushed, its reads
     report EOF: the driver shuts the stream down as it did on the server's EOF
     (`close_notify` over TLS, then the socket's write half), ends, and the socket is
     closed. Statements in flight still complete first (the client, hence Terminate, goes
     only when they are done). A fork()ed child still sends nothing; a server drop before
     Terminate is flushed ends the connection as before. Cached statements are no longer
     closed one by one on the way out: the session's end frees them, and their answers
     would arrive at a closed socket (an RST, and a log line on the server).
  2. `Connection.terminate()` (asyncpg's name and semantics): synchronous, closes at once
     without Terminate, fails pending and in-flight operations with
     `ConnectionClosedError`; idempotent, and sends nothing in a fork()ed child. A
     cancelled `close()` (e.g. `asyncio.timeout`) terminates its connection, and a
     cancelled `pool.close()` (or `async with pool` exit) terminates every connection still
     open, checked-out ones included, then re-raises, as asyncpg's `Pool.close` does. No
     `pool.close(timeout=...)`: `asyncio.timeout` around it is the bound.
  The server ends a terminated session when it next reads or writes: a statement running
  there (e.g. `pg_sleep`) runs to its end first (a terminated connection sends no cancel,
  see 6).

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

Fixed (edb7548), half-open connections: **a lost RST or a network partition was never
noticed.** If neither FIN nor RST arrives (a partition, a crashed host, macOS dropping an
RST into a closed window as above), the socket stays `ESTABLISHED`: queries waited until
the caller's timeout, and `close()` waited for them. fastpg set no `SO_KEEPALIVE`. It now takes
libpq's keys, with libpq's semantics: `keepalives` (default 1, 0 turns them off),
`keepalives_idle`, `keepalives_interval`, `keepalives_count` (`SO_KEEPALIVE`,
`TCP_KEEPIDLE` / macOS `TCP_KEEPALIVE`, `TCP_KEEPINTVL`, `TCP_KEEPCNT`) and
`tcp_user_timeout` (milliseconds, `TCP_USER_TIMEOUT`, Linux only). Absent, 0 or negative
keeps the OS default; a non-integer is an `InterfaceError` worded like libpq's, and a value
the OS refuses fails the attempt (`setsockopt(TCP_KEEPALIVE) failed: Invalid argument`).
Parsed in Rust with `connect_timeout` (`tls::split_dsn`, `dsn_socket_options`), set in
`_connect_candidate` on each TCP socket before it connects, so pool and multi-host
connections get them too; unix sockets get none. A keepalive or write timeout surfaces as
a fatal socket error, which `EofWatch` turns into `ConnectionClosedError` for everything
pending.

Not tested: that a dead peer is noticed. Keepalive probes and unacknowledged segments need
a peer whose kernel stops answering, and every forwarder here (`FaultProxy`, Docker's)
answers from its own kernel; dropping packets needs root (`pf`, `iptables`). The tests
check that every kind of connection (URL and key/value DSN, pool, multi-host, TLS) takes
the keys, that a unix socket ignores them, and the invalid values.

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
* **Unknown DSN keys are named**: `unsupported connection option "gssencmode"`; any other
  xitca parse failure says `invalid connection string: malformed, or an invalid port or
  target_session_attrs value`.

* **One connect attempt per waiting statement** (bac365b): `_route` no longer grows the
  pool for a statement it can't place, since the statement waits and its dispatch grows it.
  Against a refusing database, 20 statements in a row made 40 connect attempts, now 20;
  10 concurrent ones make at most 10 (and at most `max_size`), each failing with a connect
  error. `acquire()` already made one. A saturated pool grows as fast as before: from 1 to
  16 connections under 256 tasks took 13.7-24.0 ms (median 16.0) before, 13.3-22.1 ms
  (median 18.8) after, i.e. noise, since it mostly grows on the pipelining path, which is
  unchanged.

Open (optional): **no backoff.** With nothing open (all connections dead, or `min_size=0`),
every statement starts a connect and fails with it: a reconnect storm at the request rate
when the database comes back. A possible design: after a failed connect, no new connect for
`min(0.1 s * 2^failures, 10 s)` (with jitter); meanwhile, a statement that finds no usable
connection fails at once with the last connect error, and `_tidy` retries when the backoff
ends; a success resets it. With `connect_timeout`, an unreachable database then costs one
bounded wait per backoff step instead of one per statement.

When the database refuses new connections but some are open, growth failures are dropped
quietly and the open connections keep serving (pipelined).

## 6. Slow queries

* `asyncio.timeout(0.2)` around `pg_sleep(5)` raises on time, and the statement is
  cancelled on the server (below): the next statement on that connection runs at once. The
  same on an `acquire()` connection, in a transaction (whose ROLLBACK is sent behind the
  cancel and runs at once), in `pool.transaction()`, and for a shared statement alone on its
  pool connection (its slot is free again within milliseconds); leaving `async with` after
  the timeout doesn't wait for the sleep.
* An `UPDATE` waiting for a row lock held by another session, given up on, is cancelled
  there: it doesn't run, and commit, once the lock is released. So is an `execute_many`
  whose first chunk waits for that lock, and a `copy_in` whose first row waits for an open
  transaction that inserted the same key.
* `options=-c statement_timeout=100` in the DSN: `pg_sleep(2)` raises
  `QueryCanceledError` (57014) after 0.1 s; the connection stays usable, and a pool keeps it
  (57014 isn't session-fatal).
* Pipelined with other statements, a cancelled one is not cancelled on the server (the
  others complete, and the proxy sees no second connection): it keeps its slot, and in a
  pool of two, 20 sequential fast statements meanwhile take under 0.5 s in all (routed to
  the other connection).

Fixed (85a86ed, 90f0707): **no server-side cancel.** A statement whose caller gave up kept
running on the server, and its connection stayed busy, until it ended. Now:

* **The key.** xitca keeps the session's `BackendKeyData` private (`Session`, behind
  `Client::cancel_token()`), and its `Session::query_cancel` builds a current_thread tokio
  runtime. So `net::Framer`, which already follows every message read through `EofWatch`,
  keeps the startup's `K` (process id and secret key); `IoWatch` also records whether the
  session uses TLS.
* **When.** Only when the abandoned operation is the only one on the wire: past its gates
  and not finished. The operations there are counted, and their result futures' addresses
  summed, so with one of them the sum is its future: an O(1) check, two relaxed atomic
  additions (and subtractions) per operation. That covers `acquire()` and `transaction()`
  connections, a `Connection` with one statement in flight, and a shared pool connection
  whose load is 1; the statement methods (`select_*`, `execute`, `execute_script`), and bulk
  writes cancelled before their last chunk is handed over (they hold the connection
  exclusively, so they are alone on the wire, and their rollback waits for the cancel).
  Once the last chunk is handed over, a bulk write commits even if its caller is
  cancelled, as before (a cancel could land on its COMMIT). With several statements
  pipelined nothing is sent: the server cancels whatever the session runs, possibly another
  caller's statement.
* **How.** Python connects a socket to the address the connection reached
  (`_connect_candidate`, with the DSN's keepalive settings; the same path for a unix socket),
  bounded by `connect_timeout` together with the TLS handshake, and hands it to the native
  `Cancel`. Its task, polled on the loop thread like the connection's own (no thread, no
  tokio runtime), negotiates TLS when the connection uses TLS (`tls::start` with the
  connection's settings, `sslmode` raised to at least `require`, as libpq 17 does), writes
  the 16-byte CancelRequest (length 16, code 80877102, pid, key) and waits for the server
  to close the socket.
* **The race.** A cancel that arrives after its statement finished hits the next one. The
  postmaster signals the backend before it closes the cancel socket, and an idle backend
  ignores the signal; so from the moment a cancel begins until its socket has closed (or it
  failed before its request was written), every operation on the connection waits before
  sending anything (`Cancels::settled`), and they go out in submission order afterwards,
  as asyncpg's `_wait_for_cancellation` does. A pool's ghost no-op (`_flush_ghosts`) waits
  the same way, so the slot is held until the server is past the statement and the cancel
  is over. `test_a_late_cancel_never_lands_on_the_next_statement` (200 statements given up
  on 0-4 ms in, each followed by a 2 ms one) fails within a second without that wait.
* **Best effort.** A cancel that fails (refused, timed out, TLS failure) never raises: the
  statement runs to its end, as before. Once its request is written, the wait for the
  server's close has no time bound (asyncpg's has none either): a server that has the
  request may act on it at any time, so nothing may be sent until it closes; the wait ends
  with the connection. A fork()ed child sends nothing, nor does a closed or terminated
  connection (a terminated one drops its cancels still being opened). `close()` waits for
  the statements in flight, so a cancel begun before it still goes out and shortens that
  wait.
* **Cost.** Per operation, two relaxed atomic additions, two subtractions and an atomic
  load. The pool sweep (16 connections, 16 workers, point lookups into a Struct, release
  builds, native PostgreSQL) measured 41,656 and 41,393 req/s before, 39,530 and 39,111
  after; interleaved on a loaded host, 38,700 and 39,173 before against 36,719 and 39,643
  after: within noise.
* A statement cancelled inside a transaction now aborts it (the server's 57014), as with
  asyncpg and psycopg.

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

`uv run pytest -q tests/test_network_*.py`: 104 tests, about 60 s (the slowest, a 1 s RTT
startup, takes 10 s; the long ones are marked `@pytest.mark.timeout(60)`). The destructive
ones start the tests' own container (`fastpg-edge-net-*`) on first use, removed at the end
of the session, also when `FASTPG_DSN` is set.
