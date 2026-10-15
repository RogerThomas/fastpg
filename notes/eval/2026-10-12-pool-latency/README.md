# Pool vs single connection over a high-latency link: scripts and logs

Kept next to `notes/pool-latency.md`, which has the results. The scripts ran from a scratch
directory and read the Neon DSN from a file there; in these copies that read is replaced by
the `NEON_DSN` environment variable (nothing else changed). The DSN holds a password: keep it
out of the repository and out of logs. Every Neon query is read-only.

Build: `main` at `a2b5f85`, release build from `uv sync --group bench`. Apple-silicon Mac,
macOS 26 (Darwin 25.6), Python 3.14.7; yeet runs `async def main` on uvloop.

Run from the repository root (the scripts import `gap` / `ab` from their own directory):

```sh
export NEON_DSN='postgresql://...'   # read-only role; never commit it
uv run --group bench yeet ./notes/eval/2026-10-12-pool-latency/neon_gap.py
uv run --group bench yeet ./notes/eval/2026-10-12-pool-latency/gap.py --rounds 2
uv run --group bench yeet ./notes/eval/2026-10-12-pool-latency/ab.py --iters 60
uv run --group bench yeet ./notes/eval/2026-10-12-pool-latency/ab.py blocks --reps 10
uv run --group bench yeet ./notes/eval/2026-10-12-pool-latency/ab.py history --local --iters 300
uv run --group bench yeet ./notes/eval/2026-10-12-pool-latency/variants.py --reps 30 --only ABCE --seed 11
uv run --group bench yeet ./notes/eval/2026-10-12-pool-latency/perconn.py
uv run --group bench yeet ./notes/eval/2026-10-12-pool-latency/idle.py
uv run --group bench yeet ./notes/eval/2026-10-12-pool-latency/idle_asyncpg.py
uv run --group bench yeet ./notes/eval/2026-10-12-pool-latency/sockopts.py
```

`--local` switches any of them except `neon_gap.py` to the compose
PostgreSQL at `127.0.0.1:55432` and a 10,000-row `people` fetch. `./run_n.sh N <script> [args]`
runs a script N times in a row.

| script | what |
|---|---|
| `neon_gap.py` | the original measurement: connection block, then pool block, then asyncpg, each a median of 9 after one warmup |
| `gap.py` | per-call log of which pool connection ran it (`Pool._call` wrapped), the socket's receive window, receive buffer and bytes received (`TCP_CONNECTION_INFO`), statements cached per pool connection |
| `ab.py` | `main`: one call per target per iteration, shuffled (conn, fresh conn, pool 4/4, pool 1/1); `blocks`: the original block layout repeated, alternating which goes first; `history`: four plain connections with different histories, interleaved |
| `variants.py` | block-layout variants in shuffled order: A conn + 10 `SELECT 1`, B conn, C pool 4/4, D pool 4/4 + 10 `SELECT 1`, E pool 1/1, F conn + 0.2 s sleep |
| `perconn.py` | eight connections used round robin (so each sits idle ~0.7 s between uses) |
| `idle.py` | one warm connection; per trial an untimed fetch, then a pause (sleep, sleep with `SELECT 1` every 0.1 s, or busy-wait), then the timed fetch |
| `idle_asyncpg.py` | the idle sweep with asyncpg |
| `sockopts.py` | `TCP_NODELAY`, `SO_RCVBUF`, `SO_SNDBUF`, `SO_KEEPALIVE` of a `connect()` socket and of a pool's sockets |

Logs (`logs/`), with the command that produced each:

| log | command |
|---|---|
| `neon_run1.txt` | `gap.py --rounds 2` |
| `neon_original_x5.txt` | `run_n.sh 5 neon_gap.py` |
| `neon_interleaved1.txt` | `ab.py --iters 60` |
| `neon_blocks1.txt` | `ab.py blocks --reps 10` |
| `neon_variants1.txt` | `variants.py --reps 12` |
| `neon_variants_ACE.txt` | `variants.py --reps 25 --only ACE --seed 7` |
| `neon_variants_ABCE30.txt` | `variants.py --reps 30 --only ABCE --seed 11` |
| `neon_perconn1.txt` | `perconn.py` |
| `neon_idle.txt`, `neon_idle2.txt` | `idle.py` (the first run predates the busy-wait rows) |
| `neon_idle_asyncpg.txt` | `idle_asyncpg.py` |
| `neon_sockopts.txt` | `sockopts.py` |
| `local_interleaved.txt` | `ab.py --local --iters 300` |
| `local_interleaved2.txt` | `run_n.sh 2 ab.py --local --iters 300 --seed 5` |
| `local_history.txt` | `ab.py history --local --iters 300` |
| `local_idle.txt`, `local_idle2.txt` | `idle.py --local` (the first run predates the busy-wait rows) |
| `local_sockopts.txt` | `sockopts.py --local` |
