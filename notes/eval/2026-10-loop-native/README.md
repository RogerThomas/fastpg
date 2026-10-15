# Loop-native evaluation logs (2026-10-10)

Raw output behind decisions #57 and #58 and the "Loop-native I/O" section of
[perf-notes.md](../../../perf-notes.md). All runs used Linux containers on the compose
network (Docker Desktop on an Apple-silicon Mac, 14 CPUs / 16 GB VM), Python 3.14.7, uvloop
and PostgreSQL 18.6, container to container.

> **The variants compared here no longer exist in the code.** "inline" (`fastpg-inline`), the
> threaded path (`fastpg-struct` in the first set, `fastpg-thread-*` in the second), its
> `FASTPG_SPIN_US` busy-poll and the `FASTPG_IO` / `io=` switch were all deleted afterwards.
> fastpg now has one I/O path, the loop-native one, which is what "inline" and the
> `fastpg-struct` / `fastpg-pipelined` adapters of the second set measured. The commands
> below are a record of what ran, not something that works on the current code.

## Driver names

The same adapter name means different code in the two sets:

| set | loop-native (now the only path) | old threaded path | asyncpg |
|---|---|---|---|
| CPU quotas (top-level files) | `fastpg-inline` | `fastpg-struct`, `fastpg-pipelined` | `asyncpg-struct` |
| `pipelining/` | `fastpg-struct`, `fastpg-pipelined`, `fastpg` in `mixed_loop.txt` | `fastpg-thread-struct`, `fastpg-thread-pipelined`, `fastpg` in `mixed_thread.txt` | `asyncpg-struct`, `asyncpg` |

`*-pipelined` runs every task over one shared connection. `*-struct` uses one connection per
task (a pool of 16 or 100 in the concurrent scenario).

## CPU-quota set (top-level `*.log`)

Files are named `<shape>-spin<N>-<bench>.log`. `spin<N>` is the value of `FASTPG_SPIN_US`
for that run: it only changed the threaded path (0 = off, 200 = the old default). The
`fastpg-inline` and `asyncpg-struct` rows of a `spin0` and a `spin200` file are two
repeats of the same thing.

| shape | `docker run` flag | meaning |
|---|---|---|
| `none` | – | no limit (14 CPUs) |
| `cpuset1` | `--cpuset-cpus=0` | pinned to one core, no quota |
| `cpus1` | `--cpus=1` | CFS quota of one CPU (like a k8s `limits.cpu: 1`) |
| `cpus0.5` | `--cpus=0.5` | half a CPU |
| `cpus0.25` | `--cpus=0.25` | a quarter of a CPU |

| bench | script | what it measures |
|---|---|---|
| `point` | `bench/run.py -s point` | sequential point lookups on one connection, ops/s and latency |
| `cpu` | `bench/cpu_load.py` | process CPU per wall second and per request: steady sequential, sporadic (~100 req/s) and idle |
| `api` | `bench/open_loop.py` | open-loop Poisson arrivals at 1k, 2k, 5k and 10k req/s over a pool of 20; service latency percentiles |
| `big` | `bench/run.py -s range-10k -s concurrent` | 10,000-row fetches, and 16 concurrent tasks (only for `none` and `cpus1`) |

`_status` is the runner's progress log: one line per run with its exit code (all 0, none
retried), 1,667 s in total.

Commands, per shape and spin value (`$FLAG` from the table above). The image `fastpg-eval`
was built with `docker/Dockerfile.test` from a revision that had both the threaded path (with
the spin) and the inline path, i.e. before commit `0170365`. `bench/cpu_load.py` never landed
in `bench/`; the exact script (from commit `d1d049f`) is archived here as `cpu_load.py` and was
mounted into the image as below.

```sh
D="-d fastpg-struct -d fastpg-inline -d asyncpg-struct"
RUN="docker run --rm $FLAG --network fastpg_default \
  -e FASTPG_DSN=postgresql://fastpg:fastpg@postgres:5432/fastpg -e FASTPG_SPIN_US=$SPIN \
  -v $PWD/cpu_load.py:/src/bench/cpu_load.py:ro --entrypoint uv fastpg-eval run --no-sync yeet"
$RUN ./bench/run.py -s point $D -t 5 -w 1 -o /tmp                                    # -point
$RUN ./bench/cpu_load.py -m steady -m sporadic -m idle -t 5 $D                       # -cpu
$RUN ./bench/open_loop.py $D -r 1000 -r 2000 -r 5000 -r 10000 -t 5                   # -api
$RUN ./bench/run.py -s range-10k -s concurrent -d fastpg-struct -d fastpg-inline \
  -d fastpg-pipelined -d asyncpg-struct -t 5 -w 1 -o /tmp                            # -big
```

## Pipelining set (`pipelining/`)

Run at commit `b5e22c0`, when loop-native was the default and the threaded path was still
selectable (`connect(io="thread")`, the `fastpg-thread-*` adapters, `FASTPG_IO=thread`).

| file | what |
|---|---|
| `concurrent_cpus-<shape>_c<N>_run<R>.txt` | concurrent point lookups, `N` tasks over a pool of `N` (or one pipelined connection), 3 runs per shape |
| `regression_point_range10k_none.txt` | sequential point lookups and 10k-row fetches, no CPU limit |
| `mixed_loop.txt`, `mixed_thread.txt` (+ `.json`) | `bench/mixed.py`: 1% of requests are 20 ms queries, fast-query latency per pool configuration |
| `2026…Z.json`, `2026…Z.md` | `bench/run.py`'s own output files for the concurrent and regression runs, in run order |

The CPU limit was a compose override passed with `-f`. `cpu1.yml`:

```yaml
services:
  bench:
    cpus: 1
```

`cpu0.5.yml` is the same with `cpus: 0.5`. Commands, from the repository root:

```sh
D="-d fastpg-struct -d fastpg-pipelined -d fastpg-thread-struct -d fastpg-thread-pipelined -d asyncpg-struct"
# SHAPE=none: no override; SHAPE=1 or 0.5: F="-f cpu$SHAPE.yml"
docker compose -f docker-compose.yml $F --profile bench run --rm --no-deps \
  -v $PWD/notes/eval/2026-10-loop-native/pipelining:/out bench \
  ./bench/run.py -s concurrent -c $N $D -o /out              # N = 16, 100; three runs each
docker compose --profile bench run --rm --no-deps \
  -v $PWD/notes/eval/2026-10-loop-native/pipelining:/out bench \
  ./bench/run.py -s point -s range-10k -d fastpg-struct -d fastpg-thread-struct -d asyncpg-struct -o /out
docker compose --profile bench run --rm --no-deps bench ./bench/mixed.py                     # mixed_loop
docker compose --profile bench run --rm --no-deps -e FASTPG_IO=thread bench ./bench/mixed.py # mixed_thread
```

## The same measurements on the current code

Only the loop-native path and asyncpg are left to compare. With `cpu1.yml` (above) saved in
the repository root:

```sh
task db-up
docker compose -f docker-compose.yml -f cpu1.yml --profile bench run --rm --build bench \
  ./bench/run.py -s point -s range-10k -s concurrent -d fastpg-struct -d fastpg-pipelined -d asyncpg-struct
docker compose --profile bench run --rm --build --entrypoint uv bench \
  run --no-sync yeet ./bench/cpu_per_request.py -d fastpg-struct -d asyncpg-struct
task bench-open-loop-docker -- -d fastpg-struct -d asyncpg-struct -r 1000 -r 2000 -r 5000 -r 10000
```

`bench/cpu_per_request.py` reports process CPU per request for sequential requests, like the
`steady` rows of `cpu_load.py`.
