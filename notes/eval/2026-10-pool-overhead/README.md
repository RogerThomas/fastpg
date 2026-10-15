# Pool per-statement overhead: scripts and logs

Kept verbatim next to `notes/perf-pool.md`, which has the results. Scratch paths were
replaced by `<repo>` / `<this directory>`: set them before running.

Variants are whole `_pool.py` files in `v/` next to the scripts, loaded under another
module name so one process compares them (`installed` is the checked-out
`src/fastpg/_pool.py`). Make them from the branch's commits:

```sh
mkdir -p v
git show 1e864ae:src/fastpg/_pool.py > v/base.py    # before
git show 023428a:src/fastpg/_pool.py > v/head.py    # the four Python commits
git show b18f3a4:src/fastpg/_pool.py > v/router.py  # Router (needs the native build of b18f3a4+)
```

`mk_*.py` derive the ablation and prototype variants from those (`mk_h.py`'s pipeline-earlier
prototypes also need two Router methods that were not committed: `best_below(cap, below)`
returns `best(cap)` only if its score is below `below`, and `expected(sql)` returns the SQL's
learned cost).

| script | what |
|---|---|
| `micro_sync.py` | hand-driven, with a duck-typed fake connection: the ablations of `1e864ae` (doesn't work with `Fetcher`, which needs a real `Connection`) |
| `micro2.py` | hand-driven, real `Connection` over a fake native object: the idle-path numbers |
| `micro3.py` | uvloop, fake native answering on the next iteration: TASKS on SIZE, scoring path |
| `prof3.py` | cProfile of one `micro3.py` run |
| `cpu_pool2.py` | real database: loop-thread CPU per lookup, pinned vs pool variants, interleaved |
| `crossing.py`, `callcost.py` | timeit of Python calls, PyO3 calls, `perf_counter_ns`, `*params` calls |
| `mixed_ab.py`, `mixed_sum.py` | `bench/mixed.py` A/B across variants (swapped into `src/`, restored) |
| `run_ab.py` | `bench/run.py` A/B across variants, the same way |

Logs: `mixed1.log` / `mixed1-summary.log` (mixed.py, 3 interleaved repeats of base, Router,
h2, h2r), `run16.log` / `run100.log` (bench/run.py, 3 interleaved repeats of base, Router).
