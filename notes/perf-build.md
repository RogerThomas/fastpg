# Build-level and allocator options: what they buy

Cheap, build-only ways to cut client CPU, measured against the current release profile
(`lto = "fat"`, `codegen-units = 1`): mimalloc as the global allocator, `panic = "abort"`,
PGO, and the profile defaults (`opt-level`, debug assertions, overflow checks, `strip`).
`target-cpu=native` was out of scope (it makes wheels non-portable).

**Outcome: no build change is worth committing as it stands.** PGO is the one large win
(−17 to −28% loop CPU per row on bulk writes, −4 to −7% per row on large fetches, −2 to −4%
per point lookup), but it needs a training run against PostgreSQL inside the wheel build,
and code paths the training misses come out *slower* (+12 to +18%). Its recipe is below, for
when there is a release pipeline. mimalloc is mixed (point lookups −1 to −3%, large fetches
unchanged, `copy_in` up to +13%), and `panic = "abort"` wins 1–3% on reads at the cost of
killing the Python process on a panic.

Scripts, raw JSON lines and full reports: [notes/eval/2026-10-build/](eval/2026-10-build/README.md).

## Summary

CPU per request or per row, paired change against the base build in the same round
(median, min..max over rounds). Run 1 is 8 rounds, run 2 is 6; "A/A" is a byte-identical copy
of the base build, i.e. the noise floor.

| option | (a) load test `-w 16` / `-w 128` | (b) fetch 1k / 10k rows | (c) writes (6 cells) | verdict |
|---|---|---|---|---|
| A/A (run 1) | +0.3% / −0.1% | +0.0% / +0.9% | −1.6 to +2.2% | noise floor |
| mimalloc (v3, crate default) | −2.2% / −1.6% (run 2: −2.5% / −1.1%) | −0.7% / −0.1% (run 2: +0.0% / +0.8%) | `copy_in` **+1 to +13%**, `execute_many` ±1.5% | rejected |
| mimalloc v2 (run 2) | −1.7% / −3.3% | +0.2% / −0.1% | `copy_in` −1 to **+4.5%**, `execute_many` −2% | rejected |
| `panic = "abort"` | −1.1% / −3.4% | −3.4% / −3.3% | −2.5 to +0.6% | rejected (semantics) |
| PGO, trained on the repo's benchmarks | −2.2% / −2.7% (run 2: −4.1% / −1.9%) | −6.7% / −5.0% (run 2: −5.5% / −4.3%) | **−17 to −28%** | recipe only |
| PGO, trained without `bench/writes.py` (run 2) | −3.0% / −1.7% | −4.5% / −4.8% | **+12 to +18%** | shows the risk |
| `strip = "symbols"` | same machine code | same | same | rejected (size only) |
| `opt-level`, debug assertions, overflow checks | already 3 / off / off | | | nothing to do |

## Setup and method

* Host: Apple M3 Max (14 cores), macOS 26, Python 3.14.7, uvloop 0.23, Rust 1.96.0, shared
  with other workloads (load average 4–7 during the runs). Base: `main` at `1bc23ed`.
* Every variant is a wheel from `maturin build --release --target aarch64-apple-darwin`
  (`build.sh`), unpacked into its own directory and put first on `PYTHONPATH`, so one venv
  serves all of them. Profile changes went in through `CARGO_PROFILE_RELEASE_*` and
  `RUSTFLAGS`; mimalloc through a patched copy of the tree.
* `ab.py` runs every workload once per variant per round, each in a fresh process, in
  forward order on even rounds and reverse order on odd ones; `report.py` reports per-variant
  medians and the paired change against `base` in the same round.
* Workloads (all read-only on the native PostgreSQL, whose data was not modified):
  * **(a)** the fastpg half of a 1,000,000-lookup load test: 1,000,000 primary-key lookups of a
    `users` row (uuid, 8 text, bool, timestamptz) into a msgspec Struct, pool of 16
    connections, 16 or 128 worker tasks, after 20,000 warm-up requests. Native Homebrew
    PostgreSQL 18.6 over its Unix socket, since TCP loopback syscalls on macOS hide client
    CPU. Metric: process CPU (`time.process_time`) per request over the measured phase; user
    and system time separately from `getrusage`.
  * **(b)** `fetch.py`: `select_many("SELECT * FROM users LIMIT $1", n, into=User)` for
    n = 1,000 and 10,000, 2,000,000 rows per size per process; median per-fetch process CPU
    per row.
  * **(c)** `bench/writes.py -m 'fastpg execute_many (tuples)' -m 'fastpg copy_in (tuples)'
    -m 'fastpg copy_in (Structs)' -r 10000 -r 100000 --repeat 3` against the compose
    PostgreSQL; per process, the median loop-thread CPU per row (`time.thread_time`).
* The DSN for the socket is `postgresql://perf-test:perf-test@%2Ftmp/perf-test`. The
  libpq-style `...@/perf-test?host=/tmp` form yields no hosts in fastpg (`dsn_targets`
  returns `[]`, then "no host to connect to").

## Run 1: base, A/A, mimalloc, `panic = "abort"`, PGO (8 rounds)

Medians over rounds (min–max), and the paired change against base.

| workload | base | A/A | mimalloc | abort | PGO |
|---|---:|---:|---:|---:|---:|
| (a) `-w 16`, CPU µs/req | 7.25 (7.08–7.41) | 7.27, +0.3% | 7.11, −2.2% (−4.0..−0.3) | 7.18, −1.1% (−2.6..+3.2) | 7.10, −2.2% (−4.1..+1.3) |
| (a) `-w 16`, user µs/req (7 rounds) | 4.89 | +2.0% | −5.1% | −4.6% | −5.5% |
| (a) `-w 128`, CPU µs/req | 3.66 (3.61–3.80) | 3.66, −0.1% | 3.59, −1.6% (−6.0..+0.9) | 3.53, −3.4% (−8.3..−2.3) | 3.58, −2.7% (−6.6..+0.6) |
| (a) `-w 128`, user µs/req (7 rounds) | 3.40 | −0.1% | −2.3% | −3.3% | −2.5% |
| (b) 1k rows, CPU ns/row | 647 (628–669) | 641, +0.0% | 633, −0.7% (−7.6..+5.6) | 623, −3.4% (−7.8..+5.3) | 605, −6.7% (−11.4..−2.2) |
| (b) 10k rows, CPU ns/row | 716 (699–756) | 724, +0.9% | 712, −0.1% (−5.8..+1.4) | 685, −3.3% (−10.0..+0.1) | 677, −5.0% (−8.1..−2.9) |
| (c) `execute_many` 10k, loop ns/row | 583 (567–613) | −1.3% | −0.0% | −0.5% | 465, −20.5% (−23.8..−15.8) |
| (c) `execute_many` 100k | 561 (541–680) | −1.6% | −1.5% | −2.5% | 449, −18.3% (−34.7..−9.2) |
| (c) `copy_in` Structs 10k | 343 (335–360) | +1.1% | 385, **+13.3%** (+6.3..+14.8) | −0.8% | 266, −24.3% (−26.4..−15.7) |
| (c) `copy_in` Structs 100k | 280 (271–289) | +2.2% | 296, **+4.3%** (+1.2..+10.0) | +0.6% | 207, −26.0% (−35.2..−20.9) |
| (c) `copy_in` tuples 10k | 349 (344–356) | +0.4% | 361, +3.2% (−1.0..+5.9) | −0.5% | 283, −19.0% (−25.5..−16.0) |
| (c) `copy_in` tuples 100k | 288 (273–300) | +0.5% | 294, +1.3% (−1.3..+10.2) | −1.4% | 208, −27.7% (−30.4..−22.7) |

With 16 workers, system time per request rose slightly for every faster build (+2 to +7%):
requests finish sooner, so fewer of them share a readiness callback and a `send`.

## Run 2: PGO training sets and mimalloc v2 (6 rounds)

| workload | base | PGO | PGO without writes | mimalloc v3 | mimalloc v2 |
|---|---:|---:|---:|---:|---:|
| (a) `-w 16`, CPU µs/req | 7.26 (7.24–7.36) | 6.99, −4.1% (−4.7..−3.0) | 7.06, −3.0% (−7.1..−2.3) | 7.08, −2.5% (−5.3..−2.0) | 7.14, −1.7% (−2.8..−1.3) |
| (a) `-w 128`, CPU µs/req | 3.66 (3.58–3.74) | 3.57, −1.9% (−5.2..−0.1) | 3.56, −1.7% (−6.1..+1.2) | 3.60, −1.1% (−5.6..+1.4) | 3.56, −3.3% (−5.2..−0.2) |
| (b) 1k rows, CPU ns/row | 633 (629–637) | 598, −5.5% | 603, −4.5% | 634, +0.0% | 634, +0.2% |
| (b) 10k rows, CPU ns/row | 703 (702–709) | 673, −4.3% | 670, −4.8% | 712, +0.8% | 702, −0.1% |
| (c) `execute_many` 10k, loop ns/row | 574 | 473, −17.7% | 644, **+12.4%** | +1.0% | −1.6% |
| (c) `execute_many` 100k | 545 | 452, −17.0% | 613, **+12.7%** | −0.8% | −1.8% |
| (c) `copy_in` Structs 10k | 347 | 272, −21.6% | 395, **+14.2%** | 383, **+11.7%** | 355, +3.0% |
| (c) `copy_in` Structs 100k | 287 | 209, −27.4% | 333, **+14.1%** | 301, **+5.9%** | −1.1% |
| (c) `copy_in` tuples 10k | 345 | 278, −18.6% | 401, **+16.6%** | +2.8% | −0.1% |
| (c) `copy_in` tuples 100k | 284 | 216, −25.0% | 339, **+18.0%** | 297, **+5.1%** | 296, **+4.5%** |

## The options

### mimalloc: rejected (mixed, and unmeasured where it matters)

`mimalloc = "0.1.52"` (mimalloc v3 by default, v2 behind the `v2` feature) as
`#[global_allocator]`. It only changes Rust-side allocations: row arenas, chunk buffers,
xitca's buffers, boxed futures. Python objects (the Structs, `str`, `bytes`, lists) come from
CPython's pymalloc and are unaffected. Nothing allocated on one side is freed on the other
(PyO3 copies into Python objects), so the switch is safe.

It composes with `alloc_stats.rs`: the counting allocator delegates to `System` today; with
mimalloc it delegates to `MiMalloc` instead, and the plain `#[global_allocator]` is only
declared when `alloc-stats` is off (two would not compile). The patched file is
[`mimalloc-alloc_stats.rs`](eval/2026-10-build/mimalloc-alloc_stats.rs).

Measured: point lookups 1–3% less CPU (user time −2 to −5%), large fetches unchanged, and
`copy_in` up to 13% *more* CPU per row with v3 (up to 4.5% with v2), consistently across both
runs. The likely cause (not profiled) is the chunk buffers: `copy_in` encodes each
4,096-row chunk into a fresh `BytesMut` of about 500 KB (`rows * (2 + 12 * columns)`), which
macOS's allocator recycles, while mimalloc gives large blocks back to the OS and faults them
in again. `execute_many`'s 1,024-row chunks (about 80 KB) show no regression, which fits.
Tuning that (`purge_delay`, arena options) is no longer a simple change.

Two more reasons against adopting it now: the deployment target is Linux, where glibc's
malloc behaves quite differently from macOS's and nothing here measured it; and mimalloc in
a `dlopen`ed extension on glibc should use the `local_dynamic_tls` feature (the default
`initial-exec` TLS model draws on glibc's small static-TLS surplus, which other extensions
compete for), which slows every allocation's thread-local lookup a little. Cost: +150 KB on
the `.so`, +155 KB on the wheel, and a C compiler in the build.

### `panic = "abort"`: rejected (changes failure semantics)

PyO3 wraps every Rust function Python calls in a trampoline that runs the body under
`std::panic::catch_unwind` and turns a caught panic into `pyo3_runtime.PanicException`
(`pyo3-0.29/src/impl_/trampoline.rs`; a `PanicTrap` aborts only if an unwind would cross the
FFI boundary). With `panic = "abort"` it still compiles, but `catch_unwind` never catches:
any panic prints its message and kills the whole Python process (`SIGABRT`); the application
gets no exception to handle. fastpg's readiness callbacks run inside the event loop, which
today logs a callback's exception and carries on; with `abort`, one panic in a dependency
(xitca-postgres, rustls, an out-of-bounds index) takes down a web worker and every request it
is serving. clippy's `unwrap`/`expect`/`panic` denials make panics rare in fastpg's own code,
but not impossible, and not in its dependencies.

The gain (−1 to −3.4% on reads, nothing on writes; −450 KB on the `.so`) does not pay for
that.

### `opt-level`, debug assertions, overflow checks: already right

`cargo build --release -v` passes `-C opt-level=3 -C lto=fat -C codegen-units=1
-C strip=debuginfo` to rustc for `_native`, and neither `-C debug-assertions` nor
`-C overflow-checks` (both off, the release defaults). There is no `.cargo/config.toml` in
the repo or in `~/.cargo`, no `RUSTFLAGS`, and no profile override in `pyproject.toml`'s
`[tool.maturin]`. The editable install from `uv sync` is the same release build (its `.so`
is within 128 bytes of the release wheel's 2.97 MB).

### `strip = "symbols"`: rejected (size only)

The release build already strips debug info (`strip=debuginfo`, the default since Rust
1.77). Stripping symbols too takes the `.so` from 2.97 MB to 2.57 MB and the wheel from
1.56 MB to 1.46 MB, with identical machine code, so no CPU change. It would make macOS
`sample` profiles and panic backtraces of release builds unreadable, which is how the hot
path was profiled ([perf-hot-path.md](perf-hot-path.md)). If wheel size ever matters, strip
at publish time (`maturin build --strip`) and keep local builds symbolised.

### PGO: the one big win; recipe only

Profile-guided optimisation: an instrumented build records branch and call counts while it
runs a training workload, and the final build lays out and inlines code by those counts. No
code change.

Trained on the repo's own benchmarks (fastpg drivers only, against the compose PostgreSQL:
`bench/run.py`, `bench/writes.py`, `bench/decode_cost.py`, `bench/cpu_per_request.py -t 16`),
then measured on (a) and (b), which use a different table (`users`, uuid and text columns),
a different server and separate load-test code, so they were not part of the training:

* bulk writes: **−17 to −28%** loop CPU per row (`execute_many` and `copy_in`, tuples and
  Structs; this path was in the training);
* large fetches: **−4 to −7%** CPU per row;
* point lookups through the pool: **−2 to −4%** CPU per request (user time −2 to −6%).

The catch is the training set. Trained without `bench/writes.py`, the same build makes
writes **12–18% slower** than no PGO: code without profile counts is treated as cold and
optimised for size. So the training must cover every path a release cares about, and has to
be kept in step with the code. A profile also goes stale as the source changes: functions
whose control flow changed lose their counts (LLVM warns about the hash mismatch at build
time), so the profile is regenerated for every release build, not committed. With
`-Cllvm-args=-pgo-warn-missing-function`, the build here listed 14 functions without counts,
mostly rustls drop glue the (plain-TCP) benchmarks never run.

**Is it simple enough for release builds?** The mechanics are: two extra `maturin build`
invocations, one `llvm-profdata merge`, and about 10 minutes of training here (mostly
`bench/run.py`, whose `-t` sets it). What makes it a pipeline project rather than a flag is
that the wheel build needs a PostgreSQL to train against, and each target (manylinux x86_64
and aarch64, macOS arm64) needs its own instrumented run on that platform: a cross-compiled
target has to run its training natively or under emulation, which is slow.
Worth it once there is a release pipeline, with the training set reviewed whenever a new hot
path (a new bulk method, a new decoder) lands.

#### Recipe

Run from the repository root (the toolchain comes from `rust-toolchain.toml`). The numbers
above came from scripted equivalents (`build.sh`, `train.sh`); these exact commands were then
re-run end to end (`recipe.sh`, with `PGO` pointing at a scratch directory), and the wheel
they produced matched (run 3, 4 rounds against base: `-w 128` −3.5%, fetch −6.0% / −6.4%,
writes −17.5 to −24.9%; `recipe-report.txt`).

```sh
rustup component add llvm-tools   # once: provides llvm-profdata for the pinned toolchain
task db-up                         # the training runs against the compose PostgreSQL
uv sync --all-groups               # the benchmarks' dependencies

PGO=/tmp/fastpg-pgo
TARGET=$(rustc -vV | sed -n 's/^host: //p')     # e.g. aarch64-apple-darwin
rm -rf "$PGO" && mkdir -p "$PGO/raw"

# 1. Instrumented build. An explicit --target keeps RUSTFLAGS off build scripts and proc
#    macros, which would otherwise write profiles of their own while building.
RUSTFLAGS="-Cprofile-generate=$PGO/raw" uvx --from 'maturin>=1.15,<2' maturin build \
  --release --target "$TARGET" -m native/Cargo.toml -i .venv/bin/python -o "$PGO/instrumented"
unzip -q "$PGO"/instrumented/*.whl -d "$PGO/site"

# 2. Training: fastpg's paths in the repo's benchmarks, with the instrumented build first on
#    the path. Each process merges its counts into $PGO/raw/default_<id>_0.profraw on exit.
PYTHONPATH="$PGO/site" uv run yeet ./bench/run.py -d 'fastpg*' -t 1 -w 0.3
PYTHONPATH="$PGO/site" uv run yeet ./bench/writes.py -m 'fastpg*' -r 1000 -r 10000 --repeat 1
PYTHONPATH="$PGO/site" uv run yeet ./bench/decode_cost.py -d 'fastpg*'
PYTHONPATH="$PGO/site" uv run yeet ./bench/cpu_per_request.py -d 'fastpg*' -t 16 -n 20000

# 3. Merge the raw profiles.
"$(rustc --print sysroot)/lib/rustlib/$TARGET/bin/llvm-profdata" merge \
  -o "$PGO/fastpg.profdata" "$PGO/raw"

# 4. The optimised wheel, in dist/.
RUSTFLAGS="-Cprofile-use=$PGO/fastpg.profdata" uvx --from 'maturin>=1.15,<2' maturin build \
  --release --target "$TARGET" -m native/Cargo.toml -i .venv/bin/python -o dist
```

`bench/run.py` also writes its report to `bench/results/`; delete it if it is not wanted.
`-i .venv/bin/python` pins the interpreter (maturin otherwise follows `VIRTUAL_ENV`, which
may point at another checkout). [`cargo-pgo`](https://github.com/Kobzol/cargo-pgo) automates
steps 1, 3 and 4 for plain cargo builds but not for maturin, so it saves little here.
