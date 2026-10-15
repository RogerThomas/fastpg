# Write throughput on the loop-native driver

`execute`, `execute_many`, `insert_many` and `copy_in`, re-measured on the loop-native code
(no I/O thread, [ADR 0057](../adr/0057-loop-native-only-io-path.md)) and pushed. Compared with asyncpg (`executemany`,
`copy_records_to_table`) and psycopg 3 async (`executemany`, binary `copy`).

Raw logs, JSON and the scripts that produced them: `notes/eval/2026-10-writes-perf/`.

## Setup and method

* Host only: macOS, Apple M3 Max (14 cores), shared with other workloads (load average 7–10
  during the runs). PostgreSQL 18.6 is the compose container, reached through Docker
  Desktop's port forward (`postgresql://fastpg:fastpg@localhost:55432/fastpg`). uvloop.
* `bench/writes.py` (reworked in `baba576`): every round runs each (method, size) once, in
  the same order, after an unmeasured warm-up round, so a noisy neighbour hits all methods
  alike. Reported: the median rows/s over 5 rounds with its min–max, the median loop CPU per
  row (`time.thread_time`) and process CPU. People-shaped rows (10 columns: int, text, int,
  nullable text, float8, numeric, bool, uuid, timestamptz, nullable date) into a fresh
  `LIKE people INCLUDING ALL` table with a per-process name; `people` is only read.
* `bench/write_latency.py` (new): sequential single-row `INSERT` and `INSERT ... RETURNING`
  into a per-connection TEMP table, autocommit, interleaved rounds.
* Server CPU per row: the backend's `utime + stime` from `/proc/<pid>/stat`, read with
  `docker exec fastpg-postgres-1 cat` (read-only), around one write into a TEMP table
  (`server_cpu.py`).
* Profiles: macOS `sample` on a loop of one method (`prof_writes.py`), condensed with
  `tree.py`.

```
FASTPG_DSN=postgresql://fastpg:fastpg@localhost:55432/fastpg uv run yeet ./bench/writes.py --repeat 5
FASTPG_DSN=postgresql://fastpg:fastpg@localhost:55432/fastpg uv run yeet ./bench/write_latency.py -n 5000 --repeat 5
uv run yeet notes/eval/2026-10-writes-perf/server_cpu.py
```

## Results: before and after, against asyncpg and psycopg

100,000 rows, median of 5 (min–max). "Before" is `1e864ae` (loop-native, unchanged
writes); "after" is this branch's head. The two runs are an hour apart and the host was
busier for the second (asyncpg's `executemany`, unchanged code, fell from 269k to 227k), so
compare methods within a run.

| method | before rows/s | before loop ns/row | after rows/s | after loop ns/row |
|---|---:|---:|---:|---:|
| fastpg `execute_many` (tuples) | 81k (80–107k) | 9,398 | **237k** (218–243k) | **627** |
| fastpg `insert_many` (Structs) | 424k (395–429k) | 951 | **444k** (396–456k) | **448** |
| fastpg `copy_in` (Structs) | 1.01M (0.99–1.03M) | 654 | **995k** (0.83–1.04M) | **303** |
| fastpg `copy_in` (tuples) | 1.00M (0.88–1.06M) | 656 | 960k (0.86–1.01M) | 307 |
| asyncpg `executemany` | 269k (255–272k) | 2,023 | 227k (212–236k) | 2,282 |
| asyncpg `copy_records_to_table` | 745k (743–781k) | 1,243 | 657k (647–716k) | 1,432 |
| psycopg `executemany` | 26k | 37,799 | 28k | 35,481 |
| psycopg `copy` (binary) | 954k (899–992k) | 904 | 873k (843–928k) | 964 |

Within the "after" run:

* `execute_many` matches asyncpg's `executemany` (237k vs 227k rows/s; 266k vs 271k in the
  run right after the change, `batching.txt`) at **3.6x less loop CPU** (627 vs 2,282 ns/row).
  Before, it was at 0.30x asyncpg's throughput and 4.6x its CPU.
* `copy_in` is the fastest path measured, 1.5x asyncpg's COPY and ~1.1x psycopg's, at 3.2x
  less loop CPU than psycopg and 4.7x less than asyncpg.
* `insert_many` stays 2x asyncpg's `executemany`, now at 448 ns/row.

Smaller writes (after): 1,000 rows: `execute_many` 158k rows/s (asyncpg 155k), `copy_in`
203k (asyncpg 207k, psycopg 141k); 10,000 rows: 226k (asyncpg 227k), `copy_in` 663k (asyncpg
493k, psycopg 657k). Below ~10k rows, every method is dominated by per-call round trips.

Other row kinds for `copy_in` (after, 100k): tuples 307, Structs 303, dicts 346, pydantic
379 ns/row: reading values out of the row costs at most ~75 ns/row more than a Struct's slots.

### Single-row `execute` (`INSERT`, `INSERT ... RETURNING`)

5,000 sequential requests, median of 5 rounds (`baseline-latency.txt`, before the changes;
`final-latency.txt` after, on a busier host):

| method | p50 µs | process CPU µs/req |
|---|---:|---:|
| fastpg `execute` | 332 (340 after) | 39.9 (43.1) |
| fastpg `select_one` (RETURNING) | 333 (376) | 40.2 (47.2) |
| asyncpg `execute` | 337 (337) | 43.9 (50.7) |
| asyncpg `fetchval` (RETURNING) | 341 (384) | 45.0 (52.9) |
| psycopg `execute` | 356 (466) | 63.9 (86.1) |

Latency is the Docker Desktop round trip; fastpg uses 10–15% less CPU per request than
asyncpg. The profile (`profile-single-returning.txt`) shows nothing write-specific: of the
loop's busy time, the `send`/`recv` syscalls are the largest part, then the asyncio task
step. Parameter conversion doesn't show up.

## What bounds each path

Server (backend) CPU per row next to fastpg's loop CPU per row, TEMP table, at `78ef8e3`
(`server-cpu.txt`):

| path | rows/s | client ns/row | server ns/row |
|---|---:|---:|---:|
| fastpg `execute_many` | 267k | 631 | 3,711 |
| asyncpg `executemany` | 275k | 2,092 | 3,516 |
| fastpg `insert_many` (64-row VALUES) | 534k | 478 | 1,855 |
| asyncpg `INSERT ... SELECT FROM unnest(...)`, 1,024 rows/statement | 398k | 2,433 | 1,270 |
| fastpg `copy_in` | 1.19M | 353 | 781 |
| asyncpg `copy_records_to_table` | 709k | 1,364 | 781 |

1/throughput equals the server's CPU per row for all three fastpg paths: **they are now
server-bound**, with the loop thread busy 17–42% of the time. asyncpg's COPY and unnest are
client-bound.

Before the changes, the client was the bound:

* `execute_many` (`profile-execute_many-baseline.txt`): `recvfrom` was 49% of the loop
  thread's samples and `kevent` 35%; encoding and everything else 16%. xitca 0.4 ends every
  request with a `Sync`, the server flushes a `ReadyForQuery` per `Sync`, so each row came
  back as its own small packet, and the loop paid a wake-up, a `recv` and a poll per row
  (~9 µs).
* `copy_in` (`profile-copy_in-baseline.txt`): `Bulk.feed` was 68% of the samples, encoding
  65%. Of the encoding: `datetime.utcoffset()` calls ~14% of all samples, `Decimal` (`str()`
  plus `encode_numeric`'s digit vectors) ~20%, `UUID.int` via `getattr` 5%, the `isinstance`
  chain of the generic `convert()` ~5%. 4 Rust allocations per row.
* After (`profile-copy_in-final.txt`): `kevent` (waiting on the server) 66%; `feed` 30%, of
  which encoding is half and the socket `send` a third.

Rust heap allocations per row (`rust-allocations.txt`, `alloc-stats` build):

| path | before | after encoders | after batching |
|---|---:|---:|---:|
| `execute_many` | 7.22 | 3.20 | 0.01 |
| `insert_many` | 4.08 | 0.08 | 0.02 |
| `copy_in` | 4.00 | 0.00 | 0.00 |

## Candidates

### 1. One `Sync` per chunk for `execute_many` / `insert_many` (kept, `78ef8e3`)

Each chunk's statements (1,024 rows; for `insert_many` sixteen 64-row statements) now share
one `Sync`. xitca's `Encode` is sealed but callable: all but the last statement are encoded
by xitca (`stmt.bind(..).encode(buf)` inside `send_one_way`), checked to end with a `Sync`
and stripped of it; the last is an ordinary `execute()`, whose receiver gets the whole
response cycle. Responses route correctly (one `Sync`, one `ReadyForQuery`, one receiver).

xitca only reports a cycle's last `CommandComplete`, so the per-statement results come from a
tap in `EofWatch` (`net.rs`), which sees every byte xitca reads, in plaintext (above TLS):

* It always follows the message framing (one 5-byte header per message, from the first byte
  after SSL negotiation), so it is at a message boundary whenever a bulk write starts.
* While a bulk write collects (under its exclusive gate, from the moment its setup round
  trip is done), it records per cycle: statements completed (`CommandComplete` /
  `EmptyQueryResponse`), the summed row counts, and whether an `ErrorResponse` came.
  Notices, ParameterStatus and notifications are ignored.
* Each request of the bulk task consumes one cycle, in order. A failed cycle can be handed
  out before its `ReadyForQuery` (xitca reports the error as soon as it is parsed); it is then
  not handed out again.
* The only other requests that can be in flight under the exclusive gate are the
  fire-and-forget `Close`s of dropped cached statements; their cycles begin with
  `CloseComplete` and are skipped (unit-tested with every split of the byte stream, and
  exercised by `test_execute_many_counts_survive_statement_closes`).
* It fails safe: a successful cycle whose statement count doesn't match raises
  `InterfaceError` and rolls back, rather than return a wrong count; an error the tap can't
  place falls back to the chunk's row range.

So counts and the exact failing `row_index` are unchanged (new tests: a failure at rows 0, 1,
1022, 1023, 1024, 2047 and 2999; per-statement `UPDATE` counts across chunks; `RETURNING`;
notices from a trigger; `set_config` ParameterStatus; statement closes interleaved).

| `execute_many`, 100k | before | after |
|---|---:|---:|
| rows/s (asyncpg in the same run) | 83–92k (252k) | 266k (271k) |
| loop ns/row | 9,071 | 559 |

Measured with `bench/writes.py -m 'fastpg execute*' -m 'asyncpg exec*' -r 100000 --repeat 5`
(`encoders-writes.txt`, `batching.txt`). `insert_many` gained less (434k → 478k rows/s,
618 → 394 ns/row): its 16 `Sync`s per chunk were already few.

### 2. Per-column encoders without the generic dispatch (kept, `5439502`)

Each column's encoder was already chosen from its PostgreSQL type, but only bool, ints,
floats, text, bytea and json had fast paths; everything else went through `convert()` (an
`isinstance` chain, an owned `Param`, then `ToSql`). Added: `uuid.UUID` (the int read from its
slot), `Decimal` (`str()` borrowed, and `encode_numeric` rewritten to build the base-10000
groups in place: no digit `Vec`, no splices), `date`, naive `datetime` for `timestamp`, and
aware `datetime` for `timestamptz`, skipping `utcoffset()` when the zone is
`datetime.timezone.utc`. Exact types only; subclasses and mismatches take the old path, so
error messages are unchanged. The UTC and UUID shortcuts also apply to query parameters.

| 100k rows, loop ns/row | before | after |
|---|---:|---:|
| `copy_in` (Structs) | 654 | 297 |
| `insert_many` (Structs) | 951 | 616 |
| `execute_many` | 9,398 | 9,071 |

`bench/writes.py -m 'fastpg*' -r 10000 -r 100000 --repeat 5` (`encoders-writes.txt`).
Throughput didn't move (~1.0M rows/s for COPY): the server is the bound.

### 3. Skipping the `EAGAIN` read after a short read (rejected)

xitca reads until `WouldBlock`, so every readable event costs one extra `recv`. Marking the
socket drained after a short read (safe: asyncio's readers are level-triggered) saves it.
Three alternating A/B rounds of `execute_many` (per-row `Sync`, the most read-heavy case),
single-row writes and point lookups showed no difference beyond the noise
(raw log removed before publication; patch in `short_read.diff`): 7.6/6.0/5.9 vs 6.6/6.1/7.5 µs/row
for `execute_many`. The data `recv`s, not the `EAGAIN` ones, were the expensive part, and
batching removed most of them anyway. Worth one re-test on Linux, where syscalls are cheaper
but proportionally more of the per-read work.

It is in the code nonetheless, from the point-lookup work (`LoopSocket::read`, see
[perf-hot-path.md](perf-hot-path.md), change 1), where it saves one of four syscalls per
query. It sits below the tap (and below TLS): the skipped read returns `WouldBlock` without
data, so `EofWatch` still sees every byte read.

### 4. How `insert_many` should insert (kept: 64-row VALUES)

| option | server ns/row | rows/s | error granularity |
|---|---:|---:|---|
| 64-row `VALUES` (current) | 1,855 | 534–563k | 64 rows |
| one row per statement, batched | ~3,700 | 293k | exact row |
| 16 / 256 / 1,024-row `VALUES` | — | 536k / 575k / 554k | 16 / 256 / 1,024 rows |
| `unnest` arrays, 64 / 256 / 1,024 rows (asyncpg as client) | 1,660 / 1,465 / 1,367 | — | 64 / 256 / 1,024 rows |
| COPY under the hood | 781 | ~1.2M | exact row |

(TEMP table, `tune.py` and `server_cpu_unnest.py`, `tuning.txt` and `server-cpu.txt`.)

* Larger `VALUES` groups gain ≤ 5% and make errors vaguer: rejected.
* `unnest` saves 11% of server time at the same 64-row granularity, 26% at 1,024 rows (16x
  vaguer errors), and needs binary array encoders plus an element→array type mapping (with
  a fallback for enums and domains): not worth it.
* COPY is 2.4x cheaper for the server, but `insert_many` is documented to run real `INSERT`s
  (rules apply, COPY bypasses them). Switching would change semantics; users who want COPY
  speed call `copy_in`. Left as is; an opt-in would be an API decision.
* One row per statement would give exact error rows at half the throughput: rejected as a
  default.

### 5. Chunk sizes, pipelining depth, COPY buffers (kept the defaults)

`tune.py` patches `BulkSettings` per run (`tuning.txt`, TEMP table, median of 5):

* `execute_many`: `in_flight` 1 / 2 / 4 → 227k / 291k / 297k rows/s; chunk 256 / 1,024 /
  4,096 → 277k / 291k / 299k. One chunk in flight stalls the server between chunks; beyond
  two, nothing changes. The 3% from 4,096-row chunks is within the noise and costs 4x the
  memory and coarser cancellation: kept 1,024 and 2.
* `copy_in`: chunk 1,024 / 4,096 / 16,384 → 1.32M / 1.27M / 1.17M rows/s at 346 / 302 /
  281 ns/row; `in_flight` 1 / 2 / 4 → no difference. Smaller chunks start the server
  sooner and overlap better, at more CPU per row; 4,096 is a fair middle. With the encoder
  3–4x faster than the server, the 4 MiB back-pressure high-water mark is what paces a
  large COPY; its waits sleep on socket progress and cost no CPU, so it was left alone.

### 6. Row extraction (no change)

Structs are read from their slots, tuples by index, dicts by interned key, pydantic models
from `__dict__`: 303 / 307 / 346 / 379 ns/row for the whole COPY (after). Dataclass rows
aren't supported at all (`TypeError`); supporting them (slots via member offsets, or
`getattr`) would be an API addition, so it is left as an open question.

## Ranking

1. **One `Sync` per chunk for `execute_many`** (+ the response tap): 2.9–3.2x throughput, 15x
   less loop CPU per row, now at asyncpg's (server-bound) throughput with 3.6x less CPU.
2. **Per-column encoders**: COPY loop CPU halved (654 → 297 ns/row), `insert_many` −35%;
   Rust allocations per row 4 → 0. No throughput change on this host (server-bound), but
   it is what frees the loop for other tasks during a bulk write, and on a faster server it
   becomes throughput.
3. Defaults (chunk sizes, pipelining depth, insert group): measured, already right.
4. Rejected: the short-read `recv` skip (no effect), `unnest` and larger groups for
   `insert_many` (small gains, vaguer errors), COPY under `insert_many` (semantics).

## What needs Linux confirmation

* All rows/s figures. Docker Desktop's port forward adds per-packet cost on macOS, which
  inflated the per-row `Sync` penalty; on Linux the old `execute_many` was 117k rows/s. The
  batching gain should hold (the server-side flush per row and the client read per row both
  go away), but its size needs the Linux run, as do the README/guide tables (still from the
  threaded path).
* The server-bound conclusions (server CPU per row ≈ 1/throughput) for `insert_many` and
  `copy_in`, on a regular (WAL-logged) table.
* The short-read `recv` skip (candidate 3), where syscalls are cheaper.
* Bulk writes over TLS: the tap sits above rustls and sees plaintext, but `tests/test_tls.py`
  needs the TLS container, which wasn't started here (shared host).
