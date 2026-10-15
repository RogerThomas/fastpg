# Build-level options (2026-10-15, macOS host): scripts and raw results

> Scripts and raw logs that contained machine-specific paths were removed before publication;
> the commands below are kept for reference.

Kept next to [notes/perf-build.md](../../perf-build.md), which has the results. Host: Apple
M3 Max (14 cores), macOS 26, Python 3.14.7, uvloop 0.23, Rust 1.96.0; `main` at `1bc23ed`.
The scripts ran from a scratch directory (the absolute paths in them are that directory and
the worktree); copy them back next to each other to re-run.

| file | what |
|---|---|
| `build.sh` | builds one variant with `maturin build --release --target aarch64-apple-darwin` into its own target dir, unpacks the wheel into `variants/<name>/` |
| `train.sh` | PGO training on the instrumented build (`variants/pgo-gen`): `bench/run.py`, `bench/writes.py`, `bench/decode_cost.py`, `bench/cpu_per_request.py -t 16`, fastpg drivers only |
| `train-reads.sh` | the same without `bench/writes.py` (the `pgo-reads` variant) |
| `ab.py` | interleaved A/B: every workload once per variant per round, fresh processes, order reversed on odd rounds; appends JSON lines |
| `report.py` | per-variant medians, min–max, and the paired change against `base` |
| `fetch.py` | workload (b): `select_many` of 1,000 / 10,000 `users` rows into a Struct, CPU ns per row |
| `mimalloc-alloc_stats.rs` | `native/src/alloc_stats.rs` as patched for the mimalloc variants (plus `mimalloc = "0.1.52"` in `native/Cargo.toml`; `features = ["v2"]` for `mimalloc2`) |
| `main.jsonl`, `main-report.txt` | run 1: `base base-aa mimalloc abort pgo`, 8 rounds |
| `second.jsonl`, `second-report.txt` | run 2: `base pgo pgo-reads mimalloc mimalloc2`, 6 rounds |
| `recipe.sh` | the PGO recipe in perf-build.md run verbatim (only `PGO=` and the output directory changed) |
| `recipe.jsonl`, `recipe-report.txt` | run 3: `base pgo pgo-recipe` (the wheel `recipe.sh` built), 4 rounds, workloads `w128`, `fetch`, `writes` |

Workload (a) is the fastpg half of the untracked `load_test.py` from the main checkout, run
as `load.py -n 1000000 -c 16 -w {16,128}`: the same `User` Struct, `Budget`, id stride,
20,000-request warm-up and worker loop, without the asyncpg half and the progress bar, plus
`time.process_time` / `time.thread_time` / `getrusage` around the measured phase, printed as
one `JSON {...}` line (`cpu_us`, `user_us`, `sys_us`, `rate`, `p50_us`, `p99_us`). It is not
copied here because the original is not in the repository. `user_us` / `sys_us` were added
after run 1's first round, so run 1 has 7 rounds of them.

Variants: `base` (current profile), `base-aa` (a copy of `base`), `abort`
(`CARGO_PROFILE_RELEASE_PANIC=abort`), `strip` (`CARGO_PROFILE_RELEASE_STRIP=symbols`, size
only), `mimalloc` / `mimalloc2` (patched tree), `pgo-gen` (`RUSTFLAGS=-Cprofile-generate=...`),
`pgo` / `pgo-reads` (`RUSTFLAGS=-Cprofile-use=<merged .profdata>`).

```sh
./build.sh base
CARGO_PROFILE_RELEASE_PANIC=abort ./build.sh abort
./build.sh mimalloc /path/to/patched/tree
RUSTFLAGS="-Cprofile-generate=$PWD/pgo-raw" ./build.sh pgo-gen && ./train.sh
llvm-profdata merge -o fastpg.profdata pgo-raw
RUSTFLAGS="-Cprofile-use=$PWD/fastpg.profdata" ./build.sh pgo
cp -R variants/base variants/base-aa
yeet ./ab.py base base-aa mimalloc abort pgo -r 8 --out main.jsonl
yeet ./ab.py base pgo pgo-reads mimalloc mimalloc2 -r 6 --out second.jsonl
yeet ./report.py main.jsonl
```
