#!yeet
"""How a fetch's time depends on how long its (warm) connection sat idle before it.

Each trial: one untimed fetch (re-warms the connection), then the pause, then the timed
fetch. Pause levels are shuffled across trials. "+ping" variants send SELECT 1 every
`ping_every` seconds during the pause (a keepalive), to see whether that keeps the window.
"""

import asyncio
import random
import statistics
import time
from functools import partial

import fastpg
from ab import _setup, _time


async def _pause(conn: fastpg.Connection, seconds: float, mode: str, ping_every: float) -> None:
    if mode == "busy":  # keep this thread's CPU busy instead of sleeping (power-state control)
        end = time.perf_counter() + seconds
        while time.perf_counter() < end:
            pass
        return
    if mode == "sleep":
        await asyncio.sleep(seconds)
        return
    loop = asyncio.get_running_loop()
    end = loop.time() + seconds
    while (left := end - loop.time()) > 0:
        await asyncio.sleep(min(ping_every, left))
        if loop.time() < end:
            await conn.select_one("SELECT 1")


async def main(*, local: bool = False, trials: int = 8, seed: int = 4, ping_every: float = 0.1) -> None:
    dsn, query, into = _setup(local)
    rng = random.Random(seed)
    levels: list[tuple[float, str]] = [
        (p, "sleep") for p in (0.0, 0.05, 0.1, 0.2, 0.3, 0.5, 0.75, 1.0, 2.0)
    ] + [(0.5, "ping"), (1.0, "ping"), (0.5, "busy"), (1.0, "busy")]
    plan = [lvl for lvl in levels for _ in range(trials)]
    rng.shuffle(plan)
    results: dict[tuple[float, str], list[float]] = {lvl: [] for lvl in levels}
    async with await fastpg.connect(dsn) as conn:
        for _ in range(10):
            await conn.select_one("SELECT 1")
        fetch = partial(conn.select_many, query, into=into)
        for _ in range(3):
            await fetch()
        for pause, mode in plan:
            await fetch()
            await _pause(conn, pause, mode, ping_every)
            results[(pause, mode)].append(await _time(fetch))
    base = statistics.median(results[(0.0, "sleep")])
    suffix = {"sleep": "", "ping": f" + SELECT 1 every {ping_every}s", "busy": " (busy-wait, no sleep)"}
    for (pause, mode), ms in results.items():
        label = f"idle {pause:.2f}s" + suffix[mode]
        med = statistics.median(ms)
        print(f"{label:<36} median {med:6.1f} ms  (+{med - base:5.1f})  min {min(ms):6.1f}  max {max(ms):6.1f}")
