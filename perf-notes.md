# Performance notes

What I measured, what I changed, and what's still open. All numbers come from an Apple-silicon
Mac running Docker Desktop (14 CPUs / 16 GB VM), Python 3.14.7, uvloop, PostgreSQL 18.6, unless
stated otherwise.

Since [ADR 0057](adr/0057-loop-native-only-io-path.md) fastpg has one I/O path: the asyncio loop thread runs the protocol itself
and there is no I/O thread. The [Perf round](#perf-round-2026-10-11) section has the current
numbers; the [Loop-native I/O](#loop-native-io) section's are from the day before and are
kept as history, as are the sections that measured the old threaded path (the I/O thread,
its spin, the mailbox hand-off), which say so.

## Perf round (2026-10-11)

An interleaved A/B of `282e536` (BASE, before the round) against the `perf-round` branch at
`6b4d8f6` (NEW: the pool's `Router`, the hot-path, decode and bulk-write work in
`notes/perf-*.md`). Linux containers on the compose network, container to container, the
benchmark container under `--cpus 1` unless noted; each measurement ran BASE then NEW, in
three rounds (six for the 16-task pool), and the figures are medians. Raw logs, the summary
with ranges and the exact commands: [notes/eval/2026-10-perf-round/](notes/eval/2026-10-perf-round/README.md).

**Point lookups** (ops/s, p50 / p99 in µs):

| scenario | BASE | NEW |
|---|---:|---:|
| sequential, one connection | 14,785 · 64.2 / 104 | **15,477** · 61.8 / 106 |
| sequential, no CPU limit | 15,961 · 62.1 / 104 | 14,626 · 67.4 / 96.7 |
| 16 tasks, pool of 16 | 60,513 · 220 / 542 | **64,943** · 202 / 521 |
| 16 tasks, pinned connections | 67,272 · 194 / 497 | **69,957** · 186 / 482 |
| 100 tasks, pool of 100 | 53,927 · 1,793 / 3,602 | **57,593** · 1,629 / 3,334 |
| 100 tasks, pinned connections | 59,192 · 1,599 / 3,276 | **60,774** · 1,559 / 3,194 |
| 16 tasks, one pipelined connection | 131,904 · 114 / 214 | 129,976 · 115 / 207 |
| asyncpg, 16 tasks, pool of 16 (one run each) | 22,889 · 670 / 1,291 | 23,903 · 654 / 1,046 |

The sequential no-limit runs spread widely (NEW 14,472–19,526 ops/s), so that row shows no
change either way. The pool now costs ~1 µs per lookup over pinned connections (1.1 µs at
16 tasks, 0.9 µs at 100), down from 1.5–2 µs.

**Process CPU per sequential request** (`bench/cpu_per_request.py -n 50000`): 27.1 → **23.5 µs**
(18.0k requests/s). The default 10,000-request run measured 30.1 → 20.6 µs, with a wider
spread (20.3–29.0).

**Large results** (rows/s): msgspec Struct 1,000 rows 877k → **1.34M**, 10,000 rows 1.38M →
**1.73M**; pydantic model (direct) 1,000 rows 704k → **844k**, 10,000 rows 912k → 921k.

**Writes** (100,000 rows, rows/s, best of 3 per run):

| method | BASE | NEW | asyncpg (NEW session) |
|---|---:|---:|---:|
| `execute_many` (tuples) | 117,897 | **291,871** | 286,874 (`executemany`) |
| `insert_many` (Structs) | 506,586 | **539,578** | – |
| `copy_in` (Structs) | 1,194,743 | **1,248,657** | 746,261 (`copy_records_to_table`) |

`execute_many` is now on par with asyncpg's `executemany` (+148% over BASE), and `copy_in`
is about 1.7× asyncpg's copy. At 100,000 rows, `insert_many` and `copy_in` cost the same
from tuples, dicts, Structs or pydantic models (within 2–3%).

**Mixed load** (`bench/mixed.py`, 1% 20 ms queries, pools of 16 and 100 with up to 8 in
flight per connection, 5k–20k req/s): the fast-query p50 is 43–48 µs; in five of the six
cells fast p99 fell 9–32% and p99.9 8–82%. The exception is 100 tasks at 5k/s (p99 176 →
196 µs, p99.9 290 → 332 µs). The slow queries' p99 stayed at 25.1 ms.

**Open loop** (`bench/open_loop.py`, pool of 20): at 5k/s p99 343 → 280 µs; at 10k/s 356 →
357 µs. At 1k/s p99.9 rose from 4.8 to 9.5 ms over the three rounds, but five more rounds of
10 s each (the `ol1k` runs) measured 4.0 → 3.7 ms: at that rate the tail is dominated by the
VM's CPUs waking from idle, not by fastpg.

## Loop-native I/O

> **History:** the 2026-10-10 numbers, before the perf round above. Kept for the comparison
> with the deleted threaded path.

Linux containers on the compose network, container to container (raw logs and commands:
[notes/eval/2026-10-loop-native/](notes/eval/2026-10-loop-native/README.md)). "thread" is
the deleted threaded path, with its busy-poll off (spin 0) or at the old 200 µs default.
CPU limits were applied with `docker run --cpus` / `--cpuset-cpus`, or a compose override
setting `services.bench.cpus`.

**Sequential point lookups, one connection** (ops/s; p50 in brackets):

| CPU limit | loop-native | asyncpg | thread, spin 0 | thread, spin 200 |
|---|---:|---:|---:|---:|
| none | **15,381** (64 µs) | 14,310 (72 µs) | 9,794 (100 µs) | 18,048 (53 µs) |
| one pinned core (`--cpuset-cpus=0`) | 14,573 | 13,059 | 13,331 | 22,470 |
| `--cpus=1` | **15,357** | 13,710 | 9,946 | 14,416 |
| `--cpus=0.5` | **14,635** | 13,399 | 9,206 | 6,027 |
| `--cpus=0.25` | **9,168** | 7,191 | 4,791 | 1,740 |

The loop-native and asyncpg columns come from the spin-0 runs; the spin-200 runs repeat them
within 8%.

**Process CPU per request, sequential, one connection** (`steady` rows of `cpu_load.py`;
every thread, user and system time):

| CPU limit | loop-native | asyncpg | thread, spin 0 | thread, spin 200 |
|---|---:|---:|---:|---:|
| none | **27 µs** | 35 µs | 47 µs | 69 µs (126% of a core) |
| `--cpus=1` | **27 µs** | 34 µs | 48 µs | 71 µs |
| `--cpus=0.5` | **27 µs** | 33 µs | 47 µs | 77 µs |

Spin 200 pays for its latency with a busy core: at `--cpus=0.5` the spinning thread uses
the quota the loop thread needs, and throughput falls to 6.0k ops/s.

**Open-loop API load** (Poisson arrivals, pool of 20), p99 service latency:

| rate | CPU limit | loop-native | asyncpg | thread, spin 0 |
|---:|---|---:|---:|---:|
| 5k/s | none | **261 µs** | 659 µs | 367 µs |
| 10k/s | none | 275 µs | 920 µs | **264 µs** |
| 10k/s | `--cpus=1` | **290 µs** | 1,062 µs | 16.7 ms |
| 5k/s | `--cpus=0.5` | **4.0 ms** | 14.6 ms | 40.0 ms |
| 10k/s | `--cpus=0.5` | **12.0 ms** | 105 ms | 64.8 ms |

**Large fetch, 10,000 rows, one connection:** loop-native 1.60M rows/s, thread 1.50M,
asyncpg 0.73M (no limit). Under `--cpus=1`: 1.57M, 1.33M, 0.71M.

**Concurrent point lookups, 16 tasks** (median of 3 runs, ops/s):

| CPU limit | loop, pipelined | thread, pipelined | loop, pool of 16 | thread, pool of 16 | asyncpg |
|---|---:|---:|---:|---:|---:|
| none | **154.2k** | 122.6k | 70.3k | 75.4k | 25.2k |
| `cpus: 1` | **149.1k** | 85.7k | 70.5k | 50.7k | 25.0k |
| `cpus: 0.5` | **65.8k** | 33.7k | 32.9k | 20.3k | 11.6k |

With 100 tasks: 156.4k / 127.8k / 61.4k / 71.1k / 23.8k (none), 151.8k / 86.5k / 60.0k /
46.6k / 23.1k (`cpus: 1`), 68.0k / 34.2k / 27.5k / 18.3k / 10.7k (`cpus: 0.5`).

* Pipelining on one connection is 1.22–1.99× faster than it was on the threaded path.
* The threaded path is still ahead in one case: one connection per task with no CPU limit
  (75k vs 70k at 16 tasks, 71k vs 61k at 100), presumably because its I/O thread puts a
  second core to work on the protocol. With a CPU limit, the loop is ahead there too.
* Mixed workload (`bench/mixed.py`, 1% 20 ms queries, 5k and 10k req/s): the fast-query
  p50 is 43–59 µs on the loop path vs 84–102 µs threaded, for every configuration with
  enough connections to keep up.

## How to measure client cost (and why wall-clock alone misleads)

Through Docker Desktop's macOS port forwarding, a `SELECT 1` round trip takes **about 320 µs**.
That swamps any client-side difference for point lookups: every async driver lands at
300–400 µs. I used two other views:

* **`bench/decode_cost.py`** (`task bench-decode`) measures `time.thread_time()` on the
  event-loop thread. That's the CPU your application's own thread spends per query (protocol,
  decoding, object construction), and it's independent of network latency. It also reports total
  process CPU, which included fastpg's I/O thread when there was one.
* **`bench/cpu_per_request.py`** reports total process CPU per request, which is what a CPU
  quota (a k8s limit, a Lambda) charges for.
* **`task bench-docker`** runs the suite on Linux, in a container next to PostgreSQL, so round
  trips are container-to-container.

## Changes, with before/after

Measured on the threaded path; rows 1 and 7 no longer apply ([ADR 0057](adr/0057-loop-native-only-io-path.md) and [ADR 0058](adr/0058-delete-spin-busy-poll.md)).

| # | change | metric | before | after |
|---|---|---|---|---|
| 1 | **Mailbox + socketpair wake-up instead of `pyo3-async-runtimes`**. The I/O thread never takes the GIL; one wake-up drains every completed result. | design choice | – | – |
| 2 | **UUID slot fast path.** `uuid.UUID(int=)` runs pure-Python `__init__`. Now: `tp_alloc` + write the `int`/`is_safe` slots (offsets read from the member descriptors at startup, with a fallback). | ns per uuid cell | ~280 (Python `UUID(int=)` alone) | **52** |
| 3 | **Streamed `select_many`.** The I/O task posts every 1,024 rows (512 since `notes/perf-decode.md`), so the loop decodes while the rest is still arriving. | 10k rows → Struct, wall | 15.1 ms (slower than asyncpg's 12.6) | **7.6 ms** |
| 4 | **Numeric decode**: reusable thread-local buffer, hand-rolled digit output, vectorcall into `Decimal` (no 1-tuple). | ns per numeric cell | 142 | **117** |
| 5 | **Single-row shapes skip the temporary list** (`RowMaker` builds row 0 directly). | allocations per point lookup | +1 list | 0 |
| 6 | **Preallocated result list** (`PyList_New(n)` + `SET_ITEM`) instead of `append`. | reallocs for 10k rows | ~log₂ n | 0 |
| 7 | **Busy-poll the I/O thread for 200 µs after activity** (`FASTPG_SPIN_US`). See below. | sequential point lookup p50, Linux | 98 µs (asyncpg 72 µs) | **50 µs** |
| 8 | **EOF-aware socket wrapper** (correctness fix: xitca hangs on server close). Uses `connect_io`, which boxes the socket as `dyn AsyncIo`. | loop CPU per point lookup | 8.2 µs | 8.4 µs (no measurable change) |

## Per-cell decode cost (direct strategy, loop-thread CPU)

From the probe that selects one column type repeated 8 times over 10,000 rows
(`(row cost − baseline) / 8`):

| type | ns / cell | notes |
|---|---:|---|
| bool | 8.5 | singletons, no allocation |
| float8 | 17.5 | |
| int4 / int8 | 21 | small ints are cached by CPython |
| text | 26 | `PyUnicode_DecodeUTF8` straight from the arena (CPython validates once) |
| date | 28 | |
| timestamptz | 36 | `PyDateTime_FromDateAndTime` + cached UTC tzinfo |
| uuid | 52 | after #2 |
| numeric | 117 | after #4; most of it is CPython's own `Decimal(str)` parse and allocation |

Per-row overhead (tuple or Struct construction plus list slot) is about 35 ns. A 10-column
`Person` row costs about **490 ns**, against about **1,200 ns** for asyncpg's `Person(*record)`.

## Direct construction vs MessagePack + `msgspec.msgpack.Decoder`

Both strategies are implemented and benchmarked (`fastpg-struct` vs `fastpg-msgpack`):

| rows | direct: loop CPU | msgpack: loop CPU | direct: total CPU | msgpack: total CPU |
|---:|---:|---:|---:|---:|
| 1 | 10 µs | 11 µs | 54 µs | 55 µs |
| 1,000 | 464 µs | 968 µs | 919 µs | 1,426 µs |
| 10,000 | 4.9 ms | 9.1 ms | 8.6 ms | 12.8 ms |

Direct construction costs about **half the loop-thread CPU** of the msgpack route. The msgpack
route pays three times over: encoding in Rust, a `bytes` copy, then msgspec decoding.
msgspec's decoder must also re-parse strings, dates and decimals that Rust had already parsed
from binary. msgpack is still the right tool when a Struct needs msgspec *validation*
(constraints, enums, literals), which is why `strategy="auto"` routes only those Structs to it.

## Single-query latency on a fast network: the thread-hop cost (history)

*Historical: this measured the threaded path. Its I/O thread, `FASTPG_IO_THREADS` and
`FASTPG_SPIN_US` are gone; see [Loop-native I/O](#loop-native-io), [ADR 0057](adr/0057-loop-native-only-io-path.md) and [ADR 0058](adr/0058-delete-spin-busy-poll.md).*

Running the suite inside Linux (container-to-container, ~50 µs round trip) exposed the one
scenario where the design costs something. In a sequential point lookup, fastpg makes **two
cross-thread hops**: loop → I/O thread to send, then I/O thread → loop to deliver. asyncpg makes
none. When the I/O thread has gone to sleep between queries, waking it costs a scheduler
wake-up of ~35 µs in this VM.

| experiment | fastpg-struct p50 | asyncpg-struct p50 |
|---|---:|---:|
| baseline, 1 I/O thread | 98–105 µs | 71 µs |
| `FASTPG_IO_THREADS=2` | 109 µs | 71 µs |
| `FASTPG_IO_THREADS=4` | 107 µs | 72 µs |
| `FASTPG_SPIN_US=50` | 84 µs | 74 µs |
| **`FASTPG_SPIN_US=200` (new default)** | **50 µs** | 73 µs |
| `FASTPG_SPIN_US=1000` | 51 µs | 74 µs |

* More worker threads don't help; the problem is sleeping, not capacity.
* While spinning, the worker repeatedly yields, so tokio polls the I/O driver with a zero
  timeout. Responses are picked up immediately, and a newly spawned query doesn't need to wake
  a sleeping thread.
* 50 µs is too short a window: the application's own work between two queries is longer, so
  the thread is already asleep when the next query arrives.
* With spinning, fastpg beats asyncpg on sequential latency too, because the remaining hop is
  cheaper than asyncpg's protocol and decoding work on the loop thread.
* The cost is up to 200 µs of busy CPU on one core after the last query, so under steady
  sequential load one core is effectively dedicated. `FASTPG_SPIN_US=0` restores
  sleep-immediately behaviour.

## Client CPU per row on Linux (10,000-row fetches)

*Measured on the threaded path. The loop-thread figures still describe decoding; "total"
then included the I/O thread's protocol work, which now runs on the loop thread.*

From `bench/decode_cost.py` run inside the Linux container (the per-thread CPU clock is
precise there; on macOS it's unreliable over short intervals). "loop" is the event-loop
thread; "total" also counts fastpg's I/O thread.

| driver / route | loop ns/row | total CPU per 10k-row fetch | wall per fetch |
|---|---:|---:|---:|
| fastpg, tuples | **433** | 7.0 ms | 4.7 ms |
| fastpg, msgspec direct | **511** | 7.7 ms | 4.8 ms |
| fastpg, msgspec via msgpack | 860 | 11.1 ms | 13.1 ms |
| fastpg, msgspec via json | 907 | 11.5 ms | 13.4 ms |
| fastpg, pydantic direct | **894** | 11.5 ms | 6.7 ms |
| fastpg, pydantic via `validate_json` | 1,922 | 21.7 ms | 23.2 ms |
| asyncpg, `Person(*record)` | 1,293 | 12.9 ms | 12.9 ms |
| asyncpg, `model_validate(dict(record))` | 2,797 | 28.0 ms | 28.0 ms |
| asyncpg, `model_construct(**record)` | 3,491 | 34.9 ms | 34.9 ms |
| psycopg async, `args_row(Person)` | 1,398 | 14.0 ms | 15.7 ms |
| psycopg async, `class_row(Model)` | 2,902 | 29.0 ms | 30.6 ms |
| psqlpy, `class_row(Person)` | 2,496 | 28.0 ms | 27.0 ms |

Even counting both of its threads, the threaded path's direct route uses less total CPU than any other
driver's loop thread alone (7.7 ms vs 12.9 ms for asyncpg, for msgspec Structs).

## Pydantic

**Question: does pydantic-core have a native way to take values from Rust?** No. pydantic-core
isn't published as a Rust crate and exposes no C or Rust API. A vendored copy would be a second,
separate build whose type objects and schemas aren't shared with the user's pydantic. Its native
entry points are all Python-level `SchemaValidator` methods:
- `validate_python`: Python objects, e.g. a dict per row;
- `validate_json`: bytes parsed by jiter, in Rust, with no Python intermediates;
- `validate_strings`.

So fastpg offers two routes:

1. **`json`**: Rust writes JSON, then one `TypeAdapter(list[M]).validate_json(bytes)` call. It's
   fully validated and the fastest validated route measured (1.06–1.2× asyncpg +
   `model_validate` on large results; 1.3–3.2× at point lookups and under concurrency, because
   I/O and encoding happened off the loop on the threaded path where this was measured). Rows still have to round-trip through text JSON,
   and pydantic's JSON validation is the dominant cost (~1.9 µs/row).
2. **`direct`**: a native `model_construct`.
   - Pydantic v2 instances are plain objects with `__dict__` plus three slots
     (`__pydantic_fields_set__`, `__pydantic_extra__`, `__pydantic_private__`). fastpg allocates
     the instance with `tp_alloc`, installs a field-ordered `__dict__` via
     `PyObject_GenericSetDict`, and writes the three slots at the offsets BaseModel's member
     descriptors publish, with a generic-setattr fallback.
   - It's only enabled when walking `__pydantic_core_schema__` proves validation is a no-op
     for values of the declared types (the column types are checked once per statement).
   - Result: ~890 ns/row vs ~2,800 for `model_validate` (3.1×) and ~3,500 for Python's own
     `model_construct` (3.9×; `model_construct` is pure Python and *slower* than validating).
   - Writing the slots by offset instead of 3× `PyObject_GenericSetAttr` saved ~6% at 1k rows.

**Why pydantic stays slower than msgspec here** (~890 vs ~510 ns/row direct): each model is
three allocations (instance, `__dict__`, a per-instance `fields_set` set, which must be
mutable because pydantic adds to it on assignment) versus one for a msgspec Struct. Memory
follows: ~1.5 KB/row vs ~0.58 KB/row for the 10-column row.

## API-like load (open loop)

*Measured on the threaded path with the 200 µs spin on; current numbers are in
[Loop-native I/O](#loop-native-io).*

`bench/open_loop.py` sends point lookups as Poisson arrivals at fixed rates over a pool of
20 connections. It reports service-latency percentiles plus dispatch lag
(`bench/results/open-loop-20261010T081814Z.md`). p99 service latency:

| rate | fastpg | fastpg (1 pipelined connection) | asyncpg | psycopg async | psqlpy |
|---:|---:|---:|---:|---:|---:|
| 1k/s | 4.1 ms | 3.1 ms | 4.4 ms | 3.6 ms | 3.3 ms |
| 5k/s | 0.26 ms | 0.22 ms | 0.69 ms | 1.25 ms | 1.51 ms |
| 10k/s | 0.23 ms | 0.17 ms | 1.02 ms | 91 ms | 6.0 ms |
| 20k/s | 0.13 ms | 0.09 ms | 3.8 ms | overloaded | overloaded |
| 40k/s | 0.22 ms | 0.08 ms | overloaded | overloaded | overloaded |

* fastpg's tail latency stays flat up to 40k req/s from a single Python process. asyncpg's
  starts growing at 5k/s, reaches 20 ms at p99.9 by 20k/s, and it can't sustain 40k/s.
* At 1k/s everything is 3–5 ms at p99. That's the VM's CPUs waking from idle between sparse
  requests, the same for every driver, and the threaded path's 200 µs spin window couldn't
  bridge ~1 ms gaps. At that rate fastpg used 17–18% of the loop thread against 31–34% for asyncpg and psycopg.
* The "loop busy" column is only a fair driver comparison at low rates. Above that, the load
  generator busy-waits to hit sub-ms arrival times, inflating it equally for every driver.

## Decoder and encoder reuse (msgspec's recommendation)

Every decoder is constructed once and reused:

* **Per target type:** `plan_for` is `@cache`d and holds `msgspec.msgpack.Decoder(T)` and
  `Decoder(list[T])`, `msgspec.json.Decoder(T)` and `Decoder(list[T])`, or
  `TypeAdapter(T)` and `TypeAdapter(list[T])`.
* **Per JSON-shaped field:** a `msgspec.json.Decoder(<annotation>)` /
  `TypeAdapter(<annotation>)` is stored in the native `StructSpec` and called directly per cell.
* **Untyped JSON** (tuple rows, `Any` fields) and dict/list parameters use one process-wide
  `msgspec.json.Decoder()` / `Encoder()`. These used to be the module-level `decode`/`encode`
  functions; I measured no difference (170 vs 170 ns, 110 vs 112 ns, because msgspec caches
  its default decoder for `Any`), but switched them for consistency.

Building a typed decoder is the expensive part: msgspec compiles a type plan, and pydantic
builds a validator. That's why the caching above matters. The plan lookup per query is a
single `functools.cache` hit.

## Ideas I evaluated but didn't do (yet)

* **Writing msgspec Struct slots directly**, like the UUID trick. Rejected: it would bypass
  `__post_init__`, defaults and msgspec's GC-tracking decisions. Vectorcall into `__init__` costs
  about 100 ns per row and is the supported path.
* **`Decimal` via the `_decimal` C API.** CPython 3.14 has no stable public capsule for building
  Decimals from coefficient + exponent, so I stayed with string parsing.
* **Driving xitca's driver on the asyncio thread** (no I/O thread at all). That would remove the
  two cross-thread hops per query, the only place fastpg can lose to an in-loop driver on a
  very-low-latency network. But it would move I/O and protocol work back onto the loop thread,
  the very thing the current design avoids. Worth prototyping only if single-query latency on
  LAN-local databases turns out to matter more than throughput.
  *Done later ([ADR 0057](adr/0057-loop-native-only-io-path.md)): CPU-quota measurements made it the only I/O path.*
* **Zero-copy rows** (keeping xitca's `DataRowBody` instead of copying into the arena). The copy
  is one `memcpy` per cell (~1–2% of the per-row cost) and keeps the arena allocation-free per
  row, so it isn't worth the lifetime complexity.
* **Build-level options**: mimalloc, `panic = "abort"`, PGO, `strip` (2026-10-15,
  [notes/perf-build.md](notes/perf-build.md)). None committed: mimalloc is mixed (point
  lookups −1 to −3% CPU, `copy_in` up to +13%), `abort` turns a panic into a process abort
  for a 1–3% read gain, and `strip` only saves size. PGO cuts 17–28% of the CPU per row on
  bulk writes and 2–7% on reads, but needs a PostgreSQL to train against in the wheel build,
  and paths the training misses get slower; the recipe is in the note.
