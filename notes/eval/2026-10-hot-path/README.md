# Hot-path measurements (2026-10-11, macOS host)

Scripts and logs behind [notes/perf-hot-path.md](../../perf-hot-path.md). Host: Apple M3 Max
(14 cores), macOS 26, Python 3.14.7, uvloop 0.23, PostgreSQL 18.6 in the compose container
reached through Docker Desktop's port forward (127.0.0.1:55432). The host was shared with
other agents' benchmarks (load average 5–13), so every comparison is interleaved and
repeated; medians and spreads are reported.

| file | what |
|---|---|
| `hot.py` | point lookups into a 10-column Struct: user / sys CPU per query per round; sequential, pipelined (`pipe<T>`), pool (`pool<N>x<T>`), asyncpg |
| `ab.py` | interleaved A/B of `hot.py` over builds; paired deltas |
| `cprab.py` | interleaved A/B of the repo's `bench/cpu_per_request.py` over builds |
| `pyover.py`, `pyab.py` | Python-side cost of `select_one` with a stub native connection (no database) |
| `syscount.c`, `sysc.py` | DYLD interposer counting `recv`/`send`/`kevent`/... per query |
| `sysbench.c` | cost of an `EAGAIN` `recv` on a loopback TCP socket |
| `sagg.py` | summaries of a macOS `sample` call tree |
| `final.sh`, `final.log` | the final before/after runs (`coalesce` there is the branch head) |
| `pipe16-base-vs-1to5.log` | base vs the first five commits (no write coalescing), 16 pipelined tasks |
| `sample-breakdown.txt` | `sagg.py` output for a 60 s `sample` of sequential lookups (base + short read) |
| `sample-breakdown-final.txt` | the same for the branch head |

## Builds ("variants")

A variant is a copy of `src/fastpg` (the Python sources plus the extension built into it by
the editable install), put first on `PYTHONPATH` by the A/B scripts:

```sh
uv sync --all-groups --reinstall-package fastpg           # build the checked-out commit
mkdir -p /tmp/fastpg-variants/base && cp -R src/fastpg /tmp/fastpg-variants/base/
# alloc-stats build (Rust allocation counts in hot.py):
MATURIN_PEP517_ARGS="--features alloc-stats" uv sync --all-groups --reinstall-package fastpg
```

`base` is `1e864ae` (before this work); `final` is the branch head.

## Commands

```sh
# CPU per query, interleaved (8 pairs x 3 rounds of 4,000 sequential lookups)
uv run yeet ./notes/eval/2026-10-hot-path/ab.py base final --pairs 8 -n 4000 --mode struct
uv run yeet ./notes/eval/2026-10-hot-path/ab.py base final --pairs 6 -n 20000 --mode pipe16
uv run yeet ./notes/eval/2026-10-hot-path/ab.py base final --pairs 5 -n 20000 --mode pool1x16

# the repo's own tool, interleaved
uv run yeet ./notes/eval/2026-10-hot-path/cprab.py base final --pairs 6 --requests 5000
uv run yeet ./notes/eval/2026-10-hot-path/cprab.py base final --pairs 4 --requests 20000 \
    --tasks 16 -d fastpg-pipelined

# syscalls per query
clang -dynamiclib -O2 -o /tmp/libsyscount.dylib notes/eval/2026-10-hot-path/syscount.c
DYLD_INSERT_LIBRARIES=/tmp/libsyscount.dylib .venv/bin/yeet ./notes/eval/2026-10-hot-path/sysc.py
DYLD_INSERT_LIBRARIES=/tmp/libsyscount.dylib .venv/bin/yeet ./notes/eval/2026-10-hot-path/sysc.py --tasks 32 -n 20000
DYLD_INSERT_LIBRARIES=/tmp/libsyscount.dylib .venv/bin/yeet ./notes/eval/2026-10-hot-path/sysc.py --driver asyncpg

# Rust allocations per query (alloc-stats variant first on PYTHONPATH)
PYTHONPATH=/tmp/fastpg-variants/final-alloc uv run yeet ./notes/eval/2026-10-hot-path/hot.py -n 2000 --rounds 1

# Python-side cost of select_one (no database)
uv run yeet ./notes/eval/2026-10-hot-path/pyab.py base final

# profile: 60 s of 1 ms stack samples of a sequential run
.venv/bin/yeet ./notes/eval/2026-10-hot-path/hot.py -n 250000 --rounds 1 &
sample <pid of that python> 60 1 -file /tmp/sample.txt
uv run yeet ./notes/eval/2026-10-hot-path/sagg.py tree /tmp/sample.txt __pymethod_on_readable__

# cost of an EAGAIN recv
clang -O2 -o /tmp/sysbench notes/eval/2026-10-hot-path/sysbench.c && /tmp/sysbench
```
