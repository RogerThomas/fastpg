#!yeet
"""The idle effect with asyncpg (a different client): fetch after 0 s vs 1 s of idle."""

import asyncio
import random
import statistics
import time

import asyncpg

from ab import _setup


async def main(*, local: bool = False, trials: int = 8, seed: int = 4) -> None:
    dsn, query, _ = _setup(local)
    rng = random.Random(seed)
    plan = [p for p in (0.0, 0.5, 1.0) for _ in range(trials)]
    rng.shuffle(plan)
    results: dict[float, list[float]] = {0.0: [], 0.5: [], 1.0: []}
    conn = await asyncpg.connect(dsn)
    try:
        for _ in range(3):
            await conn.fetch(query)
        for pause in plan:
            await conn.fetch(query)
            await asyncio.sleep(pause)
            t0 = time.perf_counter()
            await conn.fetch(query)
            results[pause].append((time.perf_counter() - t0) * 1000)
    finally:
        await conn.close()
    base = statistics.median(results[0.0])
    for pause, ms in results.items():
        med = statistics.median(ms)
        print(f"asyncpg, idle {pause:.2f}s   median {med:6.1f} ms  (+{med - base:5.1f})  min {min(ms):6.1f}")
