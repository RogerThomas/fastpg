# Pool round: every driver through its own pool (2026-10-11)

Raw output behind the psycopg 3, psqlpy and asyncpg lookup figures on the docs'
Performance page, and fastpg's 100-task figure there. Linux containers on the compose network
(Docker Desktop on an Apple-silicon Mac, 14 CPUs / 16 GB VM), container to container,
against the compose PostgreSQL 18.6, with the bench container under `cpus: 1` (`cpu1.yml`).
fastpg was the build before the 2026-10-11 perf round (`../2026-10-perf-round/`).

`runner.sh` ran everything, three runs per configuration back to back, after waiting for the
host's 1-minute load average to drop to 8 or below (`raw/load.log`; each `raw/*.txt` starts
with the load it waited for):

| files | command (`./bench/run.py -s concurrent ...`) |
|---|---|
| `A-<driver>.r<n>.txt` | `-d <driver> -c 16`: 16 tasks through the driver's pool of 16 |
| `B-c<tasks>-p<size>.r<n>.txt` | `-d fastpg-struct -c <tasks> --pool-size <size>`: fastpg pool-size sweep |
| `C-<driver>.r<n>.txt` | `-d <driver> -c 100 --pool-size 16`: 100 tasks through a pool of 16 |

Medians of the three runs, ops/s:

| driver | 16 tasks, pool of 16 | 100 tasks, pool of 16 |
|---|---:|---:|
| fastpg-struct | 59,459 | 85,698 |
| asyncpg-struct | 25,299 | 23,641 |
| psycopg-async-struct | 11,607 | 10,413 |
| psqlpy-struct | 13,212 | 12,511 |
| fastpg-pipelined (one connection) | 150,571 | – |
