# Perf round A/B logs (2026-10-11)

> Scripts and raw logs that contained machine-specific paths were removed before publication;
> the commands below are kept for reference.

Raw output behind the "Perf round" section of [perf-notes.md](../../../perf-notes.md) and the
2026-10-11 figures in the docs. An interleaved A/B of two images:

| image | revision |
|---|---|
| `fastpg-base` | `282e536` (`main` before the perf round) |
| `fastpg-new` | `perf-round` at `6b4d8f6` |

Both were built with `docker/Dockerfile.test` (`build.log`). Linux containers on the compose
network (Docker Desktop on an Apple-silicon Mac, 14 CPUs / 16 GB VM), container to container,
against the compose PostgreSQL 18.6. Every measurement ran BASE then NEW back to back, after
waiting for the host's 1-minute load average to drop to 8 or below; figures are medians over
the rounds.

* `summary.md`: every metric, median (min–max) over rounds, BASE vs NEW, with the change.
* `raw/r<round>-<name>-<base|new>/`: one directory per run, `stdout.txt` plus the JSON and
  Markdown the script wrote to `bench/results/`.
* `_status.log`: one line per run with its exit code (all 0) and duration, and the load waits.
* `runner.sh`, `runner2.sh`, `runner3.sh`: the runners, in that order; `summarize.py` writes
  `summary.md` from `raw/`. They hard-code the scratch directory they ran from (`S=...`);
  point it at this directory to rerun them.

## Commands

Each run, with `$IMG` = `fastpg-base` or `fastpg-new`, `$CPU` = `--cpus 1` (empty for the
`point-nolimit` runs), `$OUT` = the run's `raw/` directory, from the repository root:

```sh
docker run --rm $CPU --network fastpg_default \
  -e FASTPG_DSN=postgresql://fastpg:fastpg@postgres:5432/fastpg \
  -v $OUT:/src/bench/results --entrypoint uv $IMG run --no-sync yeet <command>
```

`runner.sh`, rounds 1–3:

| name | command |
|---|---|
| `conc16` | `./bench/run.py -s concurrent -s concurrent-pinned -d fastpg-struct -c 16 -o /src/bench/results` |
| `conc100` | `./bench/run.py -s concurrent -s concurrent-pinned -d fastpg-struct -c 100 -o /src/bench/results` |
| `pipe16` | `./bench/run.py -s concurrent -d fastpg-pipelined -c 16 -o /src/bench/results` (round 1 also `-d asyncpg-struct`) |
| `point-cpus1` | `./bench/run.py -s point -d fastpg-struct -o /src/bench/results` |
| `point-nolimit` | the same, with no CPU limit |
| `cpureq` | `./bench/cpu_per_request.py -d fastpg-struct` |
| `range` | `./bench/run.py -s range-1k -s range-10k -d fastpg-struct -d fastpg-pydantic -o /src/bench/results` |
| `writes` (BASE) | `./bench/writes.py --repeat 3 -m 'fastpg execute_many*' -m 'fastpg insert_many (Structs)' -m 'fastpg copy_in*' -m 'asyncpg*'` |
| `writes` (NEW) | `./bench/writes.py --repeat 3 -m 'fastpg execute_many*' -m 'fastpg insert_many*' -m 'fastpg copy_in*' -m 'asyncpg*' --json /src/bench/results/writes.json` |
| `mixed` | `./bench/mixed.py -p 16 -p 100 -k 8 -o 'fastpg-pool*' -r 5000 -r 10000 -r 20000 -t 5` |
| `openloop` | `./bench/open_loop.py -d fastpg-struct -r 1000 -r 5000 -r 10000` |

`runner2.sh`: `cpureq50k`, rounds 1–3, `./bench/cpu_per_request.py -d fastpg-struct -n 50000`;
then `conc16` again in rounds 4–6, so the 16-task pool figures are medians of six.

`runner3.sh`: `ol1k`, rounds 1–5, `./bench/open_loop.py -d fastpg-struct -r 1000 -t 10`, a
recheck of the 1k/s tail (not in `summary.md`; see perf-notes.md).

The writes runs differ because BASE has no `insert_many` from tuples or dicts and no
`--json` option.
