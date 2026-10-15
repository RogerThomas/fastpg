# Large-result throughput: where a decoded row's time goes

Measured on a branch from `1e864ae`. Scripts, raw results and profile
summaries: [notes/eval/2026-10-perf-decode/](eval/2026-10-perf-decode/README.md).

**Everything here was measured on the macOS host** (Apple M3 Max, Python 3.14.7, uvloop,
the compose PostgreSQL 18.6 through Docker Desktop's port forward), on a machine shared with
other workloads (load average 8–13 throughout). No Docker benchmarks were run. Linux numbers
are still needed; see [What needs Linux confirmation](#what-needs-linux-confirmation).

## Result

Loop-thread CPU per row, including the garbage collections the fetches trigger and the
freeing of each result, 10-column `people` row, baseline `1e864ae` against branch head
`907860a`, 9 interleaved runs, median (IQR):

| rows into | 1,000-row fetch | 10,000-row fetch |
|---|---:|---:|
| msgspec `Person` | 931 (919–950) → **755** (741–767) ns, **−19%** | 787 (752–814) → **675** (656–689) ns, **−14%** |
| tuples | 855 (849–882) → **692** (689–729) ns, **−19%** | 745 (723–767) → **630** (617–646) ns, **−15%** |
| pydantic `PersonModel` | 1,300 (1,277–1,325) → **1,219** (1,180–1,226) ns, −6% | 1,288 (1,260–1,313) → **1,212** (1,183–1,255) ns, −6% |

That run happened while the host was busier than during the per-change runs below (its
baseline is 7% above theirs); the relative change is what carries over.

The brief's commands, 3 interleaved runs each, median (min–max):

| | baseline | branch | change |
|---|---:|---:|---:|
| `run.py` range-10k, fastpg-struct | 1.11M rows/s (0.81–1.12M) | **1.31M** (1.27–1.34M) | +18% |
| `run.py` range-1k, fastpg-struct | 597k rows/s (393–654k) | **685k** (596–742k) | +15% |
| `run.py` range-10k, fastpg-pydantic | 813k rows/s (694–826k) | **852k** (819–885k) | +5% |
| `run.py` range-1k, fastpg-pydantic | 486k rows/s (344–489k) | **546k** (531–549k) | +12% |
| `decode_cost.py` 10k, fastpg-struct | 703 ns/row (688–707) | **599** (598–609) | −15% |
| `decode_cost.py` 1k, fastpg-struct | 928 ns/row (892–955) | **761** (747–779) | −18% |
| `decode_cost.py` 10k, fastpg (tuples) | 684 ns/row (662–686) | **578** (568–585) | −16% |
| `decode_cost.py` 10k, fastpg-pydantic | 1,096 ns/row (1,033–1,136) | **1,003** (991–1,012) | −8% |

Wall-clock rows/s on this host is bounded by Docker Desktop's port forward as much as by the
client, so treat the `run.py` column as direction, not size.

## Commits

| commit | change |
|---|---|
| `0eb893e`, `bdb3bdc` | `bench/type_cost.py`: loop CPU per cell for each column type, GC on and off |
| `aae2f01` | `uuid.UUID` and `decimal.Decimal` values are left untracked by the cyclic GC |
| `f9d1549` | msgspec Structs built only from scalar columns are left untracked, as msgspec itself does |
| `86a9d0c` | tuple rows without json/jsonb columns are left untracked |
| `2d584f0` | `select_many` streams in 512-row chunks (was 1,024) |
| `907860a` | pydantic `__dict__`s are copied from a per-binding template instead of built key by key |

## Where a row's time goes

### Profile, 10,000-row fetches into `Person`

`sample` on a loop of fetches (`scripts/profile.sh`), inclusive samples on the loop thread
with the idle `kevent` wait removed, converted to ns with the run's measured CPU per row
(711 ns before, 611 ns after; a profiled run is slower than an unprofiled one).

| | before | after |
|---|---:|---:|
| decoding cells into Python objects, building the Struct | 40% · 282 ns | 50% · 308 ns |
|   of which `Decimal` (string render, `str`, `Decimal(str)`) | 12% · 83 ns | 14% · 88 ns |
|   of which `Struct.__init__` via vectorcall | 3% · 18 ns | 4% · 27 ns |
| **cyclic GC** (collections triggered by the rows) | **19% · 138 ns** | **1% · 7 ns** |
| freeing the previous result (the application's `del`) | 13% · 95 ns | 15% · 91 ns |
| query task: xitca parses DataRow, cells copied into the arena | 15% · 106 ns | 18% · 109 ns |
|   of which xitca's message parsing (`try_next`) | 6% · 43 ns | 8% · 49 ns |
|   of which the arena `memcpy`s and its growth | 4% · 31 ns | 6% · 35 ns |
| socket `recv` | 9% · 65 ns | 11% · 69 ns |
| event loop, futures, the rest | 4% · 25 ns | 5% · 28 ns |

On 1,000-row fetches (before): `recv` was 20% and the GC 20% of loop CPU, decoding 28%.
Docker Desktop's forward delivers about 10 KiB per read there (20 reads per fetch, 7 of
them `EAGAIN`; `raw/console.txt`), so the per-read overhead weighs more than at 10k rows.

### Per cell, by type

`uv run yeet ./bench/type_cost.py --repeats 9`: one column selected eight times over 10,000
rows, as tuples, against a zero-column `SELECT`. ns per cell, medians of 9 round-robin
samples (`raw/type_cost_*.txt`):

| type | before, GC on | before, GC off | after, GC on |
|---|---:|---:|---:|
| NULL | 14 | 11 | 10 |
| bool | 26 | 23 | 17 |
| int4, small (cached by CPython) | 32 | 30 | 24 |
| int4 | 37 | 35 | 31 |
| int8 | 42 | 39 | 35 |
| float8 | 43 | 40 | 33 |
| text (~11 chars) | 54 | 49 | 44 |
| date | 47 | 42 | 38 |
| timestamptz | 50 | 46 | 44 |
| uuid | **97** | 63 | **66** |
| numeric(12,2) | **146** | 119 | **120** |
| *per row:* zero columns | 73 | 58 | 68 |
| *per row:* `Person` as tuple | 679 | 557 | 577 |
| *per row:* `Person` as Struct | 716 | 582 | 606 |
| *per row:* `Person` as pydantic model | 1,002 | 775 | 1,009 |

* The GC-on/GC-off gap was the cost of collections: 34 ns per UUID, 28 per Decimal, and
  for the row tuple itself ~20 ns (8 bools) to ~60 ns (`Person`). After the branch it is
  gone everywhere except pydantic.
* A NULL cell costs ~10 ns: that is the whole per-cell infrastructure (xitca's range parse,
  `try_get`, the arena entry, the decode dispatch, the slot store), so no change to dispatch
  alone can save more than a fraction of it.
* `numeric` is now the most expensive cell by far: ~120 ns, a fifth of a `Person` row.
* A Struct costs ~25–30 ns per row more than a tuple of the same values.
* A pydantic row still pays ~240 ns of GC: the instance, its `__dict__` and its
  `__pydantic_fields_set__` must stay tracked (see the rejected list).

## Candidates, measured

All A/B figures: `scripts/ab.sh`, which runs `scripts/ab.py` once per (variant, target,
rows) and round, variants interleaved, each variant a self-contained copy of `src/fastpg`
plus its build on `PYTHONPATH` (`scripts/mkvariant.sh`). `ab.py` reads the loop thread's
CPU clock around a whole loop of fetches (300k rows per run) and the GC callbacks' time.
Figures are median (IQR) ns per row; "GC" is the median ns per row spent in collections.

### Kept

**1. GC: UUID and Decimal values untracked (`aae2f01`).** Both types support GC, so every
instance was tracked, although neither can be part of a cycle (a UUID holds an `int` and
`SafeUUID.unknown` and refuses `__setattr__`, a Decimal holds no object). A large fetch
keeps every row alive until it completes, so each young collection triggered while
decoding traversed all of them: one gen-0/1 collection per 10k-row fetch, 1.4 ms each for
`Person` rows, 3.3 ms for pydantic (`raw/console.txt`, `gcstats.py`). Now `decode.rs`
calls `PyObject_GC_UnTrack` on each new value (after checking `PyObject_IS_GC`). 7 runs
(`raw/ab_gc.summary.txt`):

| | baseline | leaf untracking | GC ns/row |
|---|---:|---:|---:|
| Struct, 10k | 735 (716–743) | 666 (653–669), −9.4% | 133 → 66 |
| Struct, 1k | 953 (928–961) | 897 (880–930), −5.8% | 152 → 95 |
| tuples, 10k | 697 (691–701) | 630 (623–638), −9.6% | 130 → 62 |
| pydantic, 10k | 1,174 (1,161–1,196) | 1,133 (1,099–1,141), −3.5% | 311 → 255 |

**2. GC: Structs of scalars untracked (`f9d1549`).** msgspec untracks a Struct whose values'
types don't support GC, but UUID and Decimal types do, so a `Person` stayed tracked even
with untracked values. The builder now untracks the instance when every field comes from a
non-JSON column (no defaults filled in) and the Struct is `gc=True` without `__post_init__`
or `dict=True`. Assigning a container to a field re-tracks it (msgspec's `__setattr__`, the
same path msgspec's own untracked Structs take; tested). Same run:

| | leaf untracking | + Struct untracking | GC ns/row |
|---|---:|---:|---:|
| Struct, 10k | 666 (653–669) | **595** (583–609), −10.7% | 66 → 6 |
| Struct, 1k | 897 (880–930) | **786** (770–817), −12.4% | 95 → 5 |

**3. GC: tuple rows of scalars untracked (`86a9d0c`).** `PyTuple_New` tracks; CPython only
untracks atom-only tuples later, during a collection that first traverses them (3.14
already creates `tuple(list)` results untracked). Rows with a json/jsonb column stay
tracked. 7 runs (`raw/ab_tuple.summary.txt`): tuples 10k 624 (620–631) → **575**
(572–577), −7.9%, GC 64 → 8 ns/row; tuples 1k 760 (753–762) → **683** (675–687), −10.1%.

**4. 512-row chunks (`2d584f0`).** 9 runs against 1,024 (`raw/ab_chunk3.summary.txt`):

| | 1,024 | 256 | **512** |
|---|---:|---:|---:|
| Struct, 1k | 810 (805–814) | 756 (741–759) | **745** (735–758), −8.0% |
| tuples, 1k | 759 (755–766) | 717 (715–720) | **711** (709–712), −6.3% |
| pydantic, 1k | 1,233 (1,221–1,246) | 1,165 (1,152–1,169) | **1,175** (1,157–1,182), −4.7% |
| Struct, 10k | 602 (595–604) | 601 (595–613) | **596** (593–605), −0.9% |
| tuples, 10k | 569 (557–575) | 576 (575–587) | **575** (569–578), +1.0% |
| pydantic, 10k | 1,058 (1,046–1,069) | 1,048 (1,032–1,069) | **1,049** (1,034–1,054), −0.9% |

Wall rows/s on 1k fetches went up 8–14%: the first chunk is decoded while the rest of the
result is still in flight. Smaller chunks cost more per row on 10k fetches (64: +4.6%,
128: +3.2%; `raw/ab_chunk2.summary.txt`), larger ones much more (4,096: +7.2%, 65,536,
i.e. no streaming: +18.2%; `raw/ab_io_chunk.summary.txt`), presumably from cache misses
over a bigger arena. A byte budget (e.g. "512 rows or 64 KiB") would keep wide rows in the
same range; not measured, so not done.

**5. Pydantic `__dict__` from a copied template (`907860a`).** The per-binding template
holds every key in field order, with the defaults; `PyDict_Copy` clones it at its final
size and the column values replace placeholders. Before, 10 inserts into a new dict resized
it twice (`dictresize` was 3% of pydantic samples). 9 runs (`raw/ab_pyddict.summary.txt`):
1k 1,189 (1,170–1,201) → 1,162 (1,157–1,179), −2.2%; 10k 1,077 (1,067–1,091) → 1,057
(1,043–1,062), −1.9%.

### Rejected

* **Zero-copy cells.** xitca 0.4's `Row` keeps its `DataRowBody` private, so cells can only
  be kept alive through `try_get_zc::<Bytes>` (an atomic refcount per cell, and the
  empty-value-as-NULL bug of [ADR 0008](../adr/0008-raw-row-access-via-fromsql.md)) or by reading the protocol bytes between the
  slices `RawCell` returns (pointer arithmetic on xitca internals). What they would save is
  small: the arena `memcpy`s and its growth are ~4% of samples. A safe stand-in, constant-
  size copies for 1/4/8/16-byte cells plus chunk arenas presized from the previous chunk
  (`patches/rowbuffer-fixed-copies-presized.patch`), measured 593 → 589 ns/row on 10k and
  769 → 775 on 1k (7 runs, `raw/ab_chunk2.summary.txt`): noise. Consistent with
  perf-notes' 1–2% estimate.
* **A per-column decoder plan instead of the `match`.** The whole per-cell infrastructure
  is ~10 ns (the NULL cell); the dispatch is one jump-table branch on a small `Copy` enum,
  which a fixed column pattern predicts well, and a function-pointer plan is an indirect
  branch per cell too. Not prototyped: the upper bound is a few ns per cell.
* **Treating a short read as "socket drained"** (skip the `recv` that returns `EAGAIN`,
  as tokio does; the loop's reader callback is level-triggered, so nothing is lost;
  `patches/read-short-means-drained.patch`). It removes every `EAGAIN` read, but the reads
  it defers come back on a later loop iteration with less data each: reads per 10k fetch
  83 → 80.5, per 1k fetch 20.3 → 19.3, per point lookup 2 → 1. CPU: 818 → 813 ns/row (1k),
  602 → 597 (10k), point lookups 39.9 → 39.3 µs (9 runs, `raw/ab_point.summary.txt`), all
  within noise on this host. Point lookups are where it could matter (one syscall out of
  ~27 µs of CPU per lookup on Linux); that is the latency area's call.
* **Faster `Decimal` construction.** Still the most expensive cell (~120 ns: rendering the
  digits ~13, the `str` ~11, `Decimal(str)` ~65 including `PyArg_ParseTupleAndKeywords` and
  mpdecimal's parse, then the per-cell infrastructure and freeing both). There is no public constructor that skips the string:
  `Decimal(int)` is 58 ns against 80 for `Decimal(str)` but only exact for scale 0, and
  CPython 3.14 still has no `_decimal` capsule (checked: `_decimal` exposes no `_API`).
  Filling a `PyDecObject` directly, like the UUID slot trick, would mean writing a private C
  struct with no member descriptors to verify it against; not worth the risk.
* **Caching small ints, common strings or Decimals.** CPython already caches −5..256
  (`age`: 24 ns per cell against 31 for `id`). A per-column cache keyed by the raw bytes
  would cost a hash probe per cell (~5 ns) and only pay off with repeats: break-even near a
  5% hit rate for numeric, 25–30% for short text. The benchmark's names and balances are all
  distinct, so it could only show the cost here. Worth revisiting as an opt-in or an
  adaptive per-chunk cache with real data, not as a default.
* **Untracking pydantic's objects.** In CPython 3.14 a dict is tracked from creation and is
  *not* re-tracked when a container is stored in it later (checked), so an untracked
  `__dict__` could hide a later cycle; the instance and the `fields_set` set have the same
  problem. Pydantic rows keep 3 tracked objects (from 5), ~240 ns/row of GC (24% of their
  cost) is inherent to the model's layout.
* **Other Struct construction paths.** A Struct costs ~25–30 ns over a tuple; msgspec has
  no faster public constructor, and writing its slots was already rejected ([ADR 0020](../adr/0020-structs-built-via-init.md)).
* **Rust allocations** (alloc-stats builds, `raw/console.txt`): 0.04 per row before, 0.07
  after (twice as many chunks, each regrowing its arena); not a cost that shows up. The
  presized arenas above would bring it back down at no measurable gain.

## Ranking

By CPU saved on the 10-column `Person` fetch (each step measured against the previous one):

1. Struct untracking: −11% (10k), −12% (1k), on top of 2.
2. UUID/Decimal untracking: −9% (10k), −6% (1k); also −10% for tuples and −4% for pydantic.
3. Tuple-row untracking: −8% (10k), −10% (1k) for tuple rows.
4. 512-row chunks: −5 to −8% on 1k fetches, ±1% on 10k.
5. Pydantic template dict: −2%.

Left on the table, largest first: `Decimal` construction (~14% of a `Person` row, no
public fast path), freeing the result (~15%, the application's own cost: any driver pays
it), xitca's per-row parsing plus the arena copy (~18%, inside xitca's public API), the
socket reads (~11%, set by how the bytes arrive), and pydantic's GC (~24% of a pydantic row).

## Method notes

* **`decode_cost.py` and GC.** A collection scheduled while decoding runs at the
  interpreter's next periodic check. With `decode_cost.py`'s clock reads it falls inside
  the per-fetch window (`scripts/gcwindow.py`: 30 of 30 collections inside), but with a
  bare `await` and one clock read it can fall outside, which is why `type_cost.py` and
  `ab.py` time whole loops. Freeing a result happens later still, when the event loop drops
  the future, so `decode_cost.py` never counts it: its figures are ~10% below `ab.py`'s.
* **macOS thread CPU clock.** perf-notes calls it unreliable over short intervals; over
  windows of ≥5 ms it gave IQRs of 1–3% here.
* **Noise.** The host ran other workloads throughout; each comparison interleaves its
  variants within every round, but absolute numbers drift by ±7% between comparisons.

## What needs Linux confirmation

* **Every number above.** Suggested runs, baseline `1e864ae` against the branch head:
  `task bench-docker -- -s range-1k -s range-10k -d fastpg-struct -d fastpg-pydantic -t 5`,
  and in the bench container (`docker compose --profile bench run --rm --build
  --entrypoint uv bench run --no-sync yeet ./bench/...`): `decode_cost.py -d fastpg -d
  fastpg-struct -d fastpg-pydantic -r 1000 -r 10000` and `type_cost.py --repeats 9`.
* **The GC wins' size.** Collection cost grows with the number of rows alive and with the
  process's other tracked objects (40k here); the mechanism is platform-independent, the
  ns are not.
* **The chunk size.** The 1k gain comes from overlapping decode with the result's arrival
  and from a smaller arena; container-to-container delivery and an x86 cache hierarchy may
  move the optimum. Compare 512 against 1,024 and 256 on 1k and 10k fetches.
* **The short-read heuristic on point lookups** (rejected on macOS as noise), if the
  latency work wants it.

## Found along the way (not changed)

* A `str` field fed from a json/jsonb column gets the *decoded* JSON value (a `dict` for an
  object), not the raw text that [ADR 0016](../adr/0016-direct-mode-type-compatibility.md) promises: `decode()` always JSON-decodes
  json/jsonb, and `StructPlanner` only attaches a decoder to `Kind.JSON` fields. Rows with a
  json/jsonb column are never untracked, so the GC changes are safe either way. *Fixed
  later:* the binding now marks such columns and `decode()` returns their text.
* README, `docs/architecture.md` and the chunk test comments now say 512; [ADR 0007](../adr/0007-stream-large-results.md) and
  perf-notes change #3 record the change from 1,024.
