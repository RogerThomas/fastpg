#!yeet
"""Block-layout variants, randomized per rep, to isolate what makes a fresh block slower.

Each block: open (connection or pool), optional prelude, 1 warmup fetch, `runs` timed fetches.
Prints per-variant medians of block medians, and the median time per call position.
"""

import asyncio
import random
import statistics
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from typing import Any

import fastpg
from ab import _setup, _time


@dataclass(slots=True)
class Spec:
    pool: int  # 0: a plain connection, else pool size (min = max)
    pings: int  # SELECT 1 calls before the warmup fetch
    sleep: float  # seconds to wait before the warmup fetch


VARIANTS: dict[str, Spec] = {
    "A conn +10 ping": Spec(0, 10, 0.0),
    "B conn": Spec(0, 0, 0.0),
    "C pool4": Spec(4, 0, 0.0),
    "D pool4 +10 ping": Spec(4, 10, 0.0),
    "E pool1": Spec(1, 0, 0.0),
    "F conn +sleep .2": Spec(0, 0, 0.2),
}


async def _run_block(
    fetch: Callable[[], Awaitable[object]],
    ping: Callable[[], Awaitable[object]],
    spec: Spec,
    runs: int,
) -> list[float]:
    for _ in range(spec.pings):
        await ping()
    if spec.sleep:
        await asyncio.sleep(spec.sleep)
    return [await _time(fetch) for _ in range(1 + runs)]


async def _block(dsn: str, query: str, into: type[Any], spec: Spec, runs: int) -> list[float]:
    if spec.pool:
        async with await fastpg.create_pool(dsn, min_size=spec.pool, max_size=spec.pool) as pool:
            return await _run_block(
                partial(pool.select_many, query, into=into),
                partial(pool.select_one, "SELECT 1"),
                spec,
                runs,
            )
    async with await fastpg.connect(dsn) as conn:
        return await _run_block(
            partial(conn.select_many, query, into=into),
            partial(conn.select_one, "SELECT 1"),
            spec,
            runs,
        )


async def main(*, local: bool = False, reps: int = 12, runs: int = 9, seed: int = 3, only: str = "") -> None:
    dsn, query, into = _setup(local)
    rng = random.Random(seed)
    names = [n for n in VARIANTS if not only or n[0] in only]
    results: dict[str, list[list[float]]] = {n: [] for n in names}
    for _ in range(reps):
        rng.shuffle(names)
        for name in names:
            results[name].append(await _block(dsn, query, into, VARIANTS[name], runs))
    for name in sorted(results):
        blocks = results[name]
        meds = [statistics.median(b[1:]) for b in blocks]
        warm = statistics.median(b[0] for b in blocks)
        pos = " ".join(f"{statistics.median(b[i] for b in blocks):5.1f}" for i in range(1, 1 + runs))
        print(
            f"{name:<18} block-median median {statistics.median(meds):6.2f} (p25 {statistics.quantiles(meds, n=4)[0]:6.2f}"
            f" p75 {statistics.quantiles(meds, n=4)[2]:6.2f})  warmup {warm:6.1f}  by position [{pos}]"
        )
