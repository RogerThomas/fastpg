# Pool vs single connection on a high-latency link

The question: against Neon over the internet (round trip ≈ 15–18 ms), fetching a 913-row,
21-column table into a msgspec Struct took **36.1 ms** with `conn.select_many` on one
`fastpg.connect()` connection and **41.8 ms** with `pool.select_many` on
`create_pool(dsn, min_size=4, max_size=4)` (median of 9 each, one run). Shared pool
statements run no session reset, so ~6 ms was unexplained.

Scripts, logs and the exact commands: [eval/2026-10-12-pool-latency/](eval/2026-10-12-pool-latency/README.md).
Build: `main` at `a2b5f85`. No code changed.

## Verdict

**There is no pool overhead to fix. The 5.7 ms gap is run-to-run noise.** One median of
9 calls on this link moves by several ms from one block of calls to the next. Over 82
block-layout pairs in five runs, pool 4/4 minus connection averages about **+0.5 ms**
(weighted by pairs; between −1.6 and +2.9 ms per run). Interleaved call by call, the pool was **1.6 ms
faster** (paired median). Two plain `connect()` connections differ by the same amount.
Every pool-specific hypothesis was checked, and none holds (table below).

The investigation did find a large latency effect that is real and has nothing to do with
fastpg. **A connection that sits idle for more than ~0.25 s before a large result pays for
it: +6 to +19 ms after 0.3–0.5 s idle, +45 to +56 ms after 1 s.** The data's sender
(Neon's side) restarts TCP slow start after idle. asyncpg pays the same.

## Hypotheses

| # | hypothesis | evidence | verdict |
|---|---|---|---|
| 1 | The pool's `select_many` path differs from `Connection.select_many` | `Pool._query` calls `Fetcher.fetch`, which is `_Queries._fetch`: the same method `Connection.select_many` awaits, so it submits the same native fetch and builds the Structs the same way. The pool adds `Router.begin` / `finish`, ~0.5 µs per statement (`notes/perf-pool.md`). Locally, interleaved over 300 calls each: pool 4/4 minus connection, paired median **+0.06 / +0.11 ms** in two runs. A second plain connection minus the first: +0.07 / +0.27 ms. | no |
| 2 | The pool rotates connections instead of reusing the hot one | The connection that ran each call was logged (`Pool._call` wrapped). Pool 4/4: `3333333333` in every block, warmup included. Pool 1/1: `0000000000`. `_finish` puts the entry back on top of the idle stack, and the next `_query` pops it (LIFO). `tests/test_pool.py::test_reuses_the_most_recently_idle_connection` already pins this. | no |
| 3 | Each pool connection prepares the statement on first use | `cached_statements` per pool connection after a block: `[0, 0, 0, 1]`. Only the connection that ran the warmup prepared it, and every timed call ran there. | no |
| 4 | Routing or cost learning delays dispatch, e.g. waiting for a background open | `pool.stats()` after the runs: `size=4, opening=0, idle=4`. The idle path (rule 1) never calls `_grow` and never waits. | no |
| 5 | Socket options, buffers, read sizes | `connect()` sockets and pool sockets are identical: `TCP_NODELAY` on, `SO_RCVBUF` 131072, `SO_SNDBUF` 131768 (to Neon), no `SO_KEEPALIVE`. Both open through the same `connect()`, and both read through the same native code. The client's receive window auto-tunes to 417–455 KB after the first fetch and usually to ~0.9–1 MB after the second, on both. A fetch is 251 KB on the wire, TLS included, so the receive window doesn't limit it. | no |

### The 5.7 ms gap, re-measured (Neon, ms)

The "conn" side is a connection opened, given 10 × `SELECT 1`, a warmup fetch and 9 timed
fetches, as in the original script. The "pool 4/4" side is a fresh pool, a warmup and 9
timed fetches.

| log | layout | pairs | conn | pool 4/4 | pool − conn |
|---|---|---:|---:|---:|---|
| `neon_original_x5` | the original script, 5 runs | 5 | 38.9 | 37.3 | per run +0.3, −3.7, +1.7, +1.2, +0.7 |
| `neon_blocks1` | conn and pool blocks, alternating which goes first | 10 | 37.15 | 40.00 | median +2.7, range **−2.0 … +9.3** |
| `neon_variants1` | 6 variants, shuffled | 12 | 37.22 | 36.60 | −0.6 |
| `neon_variants_ACE` | 3 variants, shuffled (a noisier hour: p75 ≈ 90 ms) | 25 | 43.80 | 43.92 | +0.1 |
| `neon_variants_ABCE30` | 4 variants, shuffled | 30 | 35.89 | 36.85 | +1.0 (plain conn without pings +0.4, pool 1/1 +0.1) |
| `neon_interleaved1` | one call each per iteration, shuffled | 60 | 40.07 | 37.14 | paired median **−1.6** (pool slower in 20/60) |

Medians of block medians (the interleaved row: medians of calls). One pair differed by
+9.3 ms and another by −2.0 ms (`neon_blocks1`), so the original single +5.7 ms pair is
within the spread. `variants.py` also checked the original script's other asymmetries:
10 × `SELECT 1` before the first fetch, and a 0.2 s pause. No variant's block median moved
by more than ~1.5 ms.

The local comparison separates per-call cost from latency: a 10,000-row fetch of `people`
(1.3 MB) over the Docker port forward, interleaved 300 calls each, three runs.

| local, ms | conn | fresh conn | pool 4/4 | pool 1/1 |
|---|---:|---:|---:|---:|
| run 1 | 8.01 | 6.48 | 6.46 | 6.44 |
| run 2 | 6.33 | 6.42 | 6.41 | 6.45 |
| run 3 | 6.33 | 6.61 | 6.37 | 6.49 |

The pool sits within 0.1 ms of a plain connection. In run 1 that particular "conn" was 1.5 ms
slower than the other three, a per-connection effect: `ab.py history` (four connections
with different histories) found no pattern in it.

## What did show up: the server restarts slow start after idle

This is `idle.py`: one warm connection, an untimed fetch, then a pause, then the timed
fetch. Pause levels are shuffled, 8 trials each. The table shows the median minus the
no-pause median (40.0 ms and 41.6 ms in the two runs).

| before the fetch | run 1 | run 2 |
|---|---:|---:|
| idle 0.05 s | −0.2 | −0.3 |
| idle 0.1 s | +4.8 | −1.8 |
| idle 0.2 s | +2.5 | −0.5 |
| idle 0.3 s | +9.3 | +6.1 |
| idle 0.5 s | +19.3 | +12.5 |
| idle 0.75 s | +38.9 | +25.3 |
| idle 1.0 s | **+55.6** | **+45.1** |
| idle 2.0 s | +49.2 | +59.3 |
| 0.5 s, with `SELECT 1` every 0.1 s | +6.5 | +3.4 |
| 1.0 s, with `SELECT 1` every 0.1 s | +2.1 | **−1.9** |
| 0.5 s busy-waiting (the client CPU never sleeps) | – | +14.7 |
| 1.0 s busy-waiting | – | +43.4 |

asyncpg, the same sweep (`idle_asyncpg.py`): +13.3 ms after 0.5 s and +41.2 ms after 1.0 s,
from a 35.5 ms base. Eight connections used in turn (`perconn.py`) sit idle ~0.7 s each
between uses: their fetch medians were **66–94 ms**, against ~37 ms back to back.

Why this is the sender's TCP congestion window:

* The pattern fits Linux's default `net.ipv4.tcp_slow_start_after_idle = 1`. After an idle
  period longer than the retransmission timeout (at least 200 ms), the window halves once
  per timeout elapsed, down to the initial 10 segments. The 251 KB result then needs a few
  more round trips of slow start. Hence little or nothing up to 0.2 s, a cost that grows
  with the idle time, and a plateau of ~3 round trips (+45–60 ms) from 1–2 s.
* It is the server's side. The data flows server to client. Keeping the client's CPU busy
  doesn't help, so it isn't power management on this Mac. A tiny reply from the server
  every 0.1 s does help: it refreshes the sender's last-send time, so the window is never
  restarted. Neon's kernel settings can't be read from here, so this part is inferred.
* Every client pays it: asyncpg's numbers match.

Locally, a different idle effect: a 1.3 MB fetch is +7 to +17 ms slower after ≥ 0.1 s idle.
`SELECT 1` every 0.1 s doesn't help there. Busy-waiting the client cuts it by about a third
(+8.7 ms against ~+13 ms). That points to power state, of this Mac's CPU and of Docker
Desktop's VM, rather than TCP. Local benchmarks with pauses between calls measure that, not
fastpg.

A new connection's first fetch takes 107–120 ms on Neon, against ~36 ms (the "warmup"
column of `variants.py`). That is one round trip to prepare the statement, plus slow start
from the initial window. It is the same for `connect()` and for a pool connection. The pool
pays it whenever it opens one: growth, and replacement after `max_lifetime` / `max_idle`.

## What the pool already does about it, and options it doesn't take

The pool's idle stack is LIFO. Sequential and lightly concurrent traffic therefore stays
on the most recently used connection, which is the one least likely to be past the idle
threshold. A FIFO queue would multiply each connection's idle time by the pool size, which
is what `perconn.py` simulates. The pool needs no change.

None of the following is implemented. The measurements don't show a clear enough win for
fastpg to take any of them on by default.

1. **Server-side `net.ipv4.tcp_slow_start_after_idle = 0`** on the host that sends the
   rows: PostgreSQL's, or a pgbouncer's or proxy's in front of it. This is the actual fix,
   at no cost to clients. It is out of reach on Neon, and possible on self-hosted servers.
2. **Keep-warm pings from the client:** a `SELECT 1` on each idle connection more often
   than the server's retransmission timeout. Measured: it removes the penalty (1.0 s idle:
   +45…+56 ms → −1.9…+2.1 ms). The costs:
   * ~10 statements per second per warm connection;
   * on serverless databases (Neon), it keeps the compute awake and defeats scale-to-zero,
     which costs money;
   * it only pays off when the next large result arrives within the warm window, after an
     idle gap of ~0.3 s to a few seconds. Small results don't benefit, since a few segments
     fit in any window;
   * a pool would need a timer per connection, and a time bound to stop pinging.

   If wanted, make it an opt-in pool setting measured on the target workload, not a
   default.
3. **Client socket tuning** (`SO_RCVBUF`, TCP options): not expected to help. The client's
   receive window is already larger than the result after one fetch, and the window that
   shrinks is the server's.

## Side observation: asyncpg's pool

In the same runs (`neon_original_x5`), asyncpg's pool was **+11 to +25 ms** over its single
connection: +17.1, +24.8, +21.3, +11.2, +21.5. That is about one round trip, the session
reset asyncpg runs on release. fastpg's shared statements skip it.
