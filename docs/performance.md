# Performance

Linux containers (Docker Desktop on an Apple-silicon Mac) with a one-CPU quota (`--cpus 1`),
PostgreSQL 18.6 in its own container, uvloop, Python 3.14; lookups go through each driver's
own pool of 16 connections; rows are decoded into a 10-column msgspec Struct with plain field
types (fastpg's direct path; fields with `Meta` constraints, enums or literals are validated
by msgspec and cost about twice the CPU on large results); medians of 3 runs.

| | fastpg | asyncpg | psycopg 3 | psqlpy |
|---|---:|---:|---:|---:|
| Lookups by id, 16 concurrent (ops/s) | 64.9k | 25.3k | 11.6k | 13.2k |
| Lookups by id, 100 concurrent (ops/s) | 135k¹ | 23.4k¹ | 10.9k¹ | 12.9k¹ |
| 10,000-row fetch (rows/s) | 1.73M | 0.71M² | – | – |
| `execute_many`, 100,000 rows (rows/s) | 292k | 287k | – | – |
| COPY, 100,000 rows (rows/s) | 1.25M | 746k | – | – |
| CPU per request, sequential lookups | 23.5 µs | 34 µs² | – | – |

¹ All four drivers in one session on 2026-10-12, current build (fastpg 134.3k–135.6k over
the 3 runs).

² One run, on 2026-10-10, with the same setup.

## Caveats

* We wrote these benchmarks, and they cover narrow query shapes: lookups by primary key,
  range scans and simple column types, against a server tuned so that the client is the
  bottleneck.
* One query at a time on one connection, fastpg's median latency is only about 10 µs below
  asyncpg's (62 µs against 72 µs); the large gaps need concurrency or large results.
* Your hardware and queries will differ: rerun them.

## Reproduce

```sh
task db-up
printf 'services: {bench: {cpus: 1}}\n' > cpu1.yml
cpu1() { docker compose -f docker-compose.yml -f cpu1.yml --profile bench run --rm --build "$@"; }
cpu1 bench ./bench/run.py -s concurrent -s range-10k -c 16 --pool-size 16 \
  -d fastpg-struct -d asyncpg-struct -d psycopg-async-struct -d psqlpy-struct
cpu1 bench ./bench/writes.py -r 100000 \
  -m 'fastpg execute_many*' -m 'fastpg copy_in*' -m 'asyncpg*'
cpu1 --entrypoint uv bench run --no-sync yeet ./bench/cpu_per_request.py -n 50000
```

Use `-c 100` for the 100-task row. The raw logs and exact commands are in `notes/eval/` in the
repository.
