# Decode-throughput evaluation logs (2026-10-11)

> Scripts and raw logs that contained machine-specific paths were removed before publication;
> the commands below are kept for reference.

Raw output behind [notes/perf-decode.md](../../perf-decode.md). Everything ran on the macOS
host (Apple M3 Max, Python 3.14.7, uvloop) against the compose PostgreSQL 18.6 through
Docker Desktop's port forward, while other agents were using the same machine.

The scripts are kept verbatim: they hard-code the agent's worktree and scratch paths
(`WT=`, `SP=` at the top of each shell script) and need those adjusted to run elsewhere.

## Variants

Each variant is a self-contained copy of `src/fastpg` (the Python sources at a commit) plus
an extension built from a commit, put first on `PYTHONPATH`, so variants with different
Python sources can be interleaved without rebuilding (`scripts/build.sh` builds with
`maturin develop --release` and saves the `.so`, `scripts/mkvariant.sh` assembles the
package).

| variant | code |
|---|---|
| `baseline` | `1e864ae` |
| `leaf` | `aae2f01` (UUID/Decimal untracked) |
| `structgc` | `f9d1549` (+ Structs untracked) |
| `tuplegc` | `86a9d0c` (+ tuple rows untracked) |
| `chunk<N>` | `86a9d0c` with `CHUNK_ROWS = N` (`scripts/chunks.sh`) |
| `shortread` / `shortread2` | `86a9d0c` / `2d584f0` + `patches/read-short-means-drained.patch` |
| `rowbuf` | `86a9d0c` + `patches/rowbuffer-fixed-copies-presized.patch` |
| `chunk512_commit` | `2d584f0` (512-row chunks) |
| `pyddict` | `907860a` (pydantic template dict) |
| `final` | `907860a` |
| `*_alloc` | the same commits built with `--features alloc-stats` (`scripts/build_alloc.sh`) |

## Files

* `raw/ab_*.jsonl`: one line per `scripts/ab.py` run (loop CPU ns/row over a whole loop of
  fetches, wall rows/s, GC ns/row and collections); `raw/ab_*.summary.txt` is
  `scripts/summ.py` over it (median, IQR, min–max per variant).
  * `ab_gc`: baseline, leaf, structgc; tuple/struct/pydantic; 1k and 10k rows; 7 rounds.
  * `ab_tuple`: structgc against tuplegc, tuples, 7 rounds.
  * `ab_io_chunk`: tuplegc, shortread, chunk256/4096/65536; Struct; 7 rounds.
  * `ab_chunk2`: tuplegc, chunk64/128/256/512, rowbuf; Struct; 7 rounds.
  * `ab_chunk3`: tuplegc, chunk256, chunk512; all targets; 9 rounds.
  * `ab_pyddict`: chunk512_commit against pyddict; pydantic; 9 rounds.
  * `ab_point`: chunk512_commit against shortread2; 1-row fetches (5,000 per run); 9 rounds.
  * `ab_final`: baseline against final; all targets; 9 rounds.
* `raw/runpy/{baseline,final}/`: `bench/run.py -s range-1k -s range-10k -d fastpg-struct
  -d fastpg-pydantic -t 3` output, 3 interleaved runs each (`scripts/final_bench.sh`).
* `raw/decode_cost_{baseline,final}.txt`: `bench/decode_cost.py -d fastpg -d fastpg-struct
  -d fastpg-pydantic -r 1000 -r 10000`, 3 interleaved runs each.
* `raw/runpy_decode_cost.summary.txt`: `scripts/summ_bench.py` over the two above.
* `raw/type_cost_{baseline,final}.txt`: `bench/type_cost.py --repeats 9`.
* `raw/console.txt`: one-off probes (GC collections per fetch, reads per fetch with an
  instrumented build, Rust allocations, `timeit` of `Decimal` constructors).
* `profiles/*.inclusive.txt`: inclusive sample counts per function (`scripts/inclusive.py`)
  from macOS `sample` on `scripts/prof_loop.py` (`scripts/profile.sh`): 10k-row Struct,
  1k-row Struct and 10k-row pydantic fetches on the baseline, 10k-row Struct on final.
* `patches/`: the two rejected prototypes, as diffs against the commit named above.
