# 100 concurrent lookups through each driver's pool of 16 (2026-10-12)

Build: `main` at ecb27dc, image built with
`docker build -t fastpg-main -f docker/Dockerfile.test .`. Three runs, uvloop, `--cpus 1`,
all four drivers in each run. `results.txt` holds the raw table rows (ops/s, then latency
columns in µs).

Each run:

```sh
docker run --rm --cpus 1 --network fastpg_default \
  -e FASTPG_DSN=postgresql://fastpg:fastpg@postgres:5432/fastpg \
  -e BENCH_LOOP=uvloop -e BENCH_C=100 -v "$PWD/notes/eval/2026-10-12-pool-c100:/w" \
  -w /src --entrypoint uv fastpg-main run --no-sync python /w/run_loop.py
```

`run_loop.py` calls `bench/run.py`'s `main` with the concurrent scenario and a pool of 16;
`BENCH_LOOP=asyncio` runs it on the standard event loop instead (it writes its output to
`/out`, so also mount a directory there).
