#!yeet
"""cProfile one loop-driven run (see micro3.py) of a pool variant."""

import asyncio
import cProfile
import pstats
import sys
from pathlib import Path
from typing import Any

import uvloop

sys.path.insert(0, str(Path(__file__).resolve().parent))
import micro3  # noqa: E402


async def _run(tasks: int, size: int, n: int, variant: str) -> None:
    loop = asyncio.get_running_loop()
    mod = micro3._load(variant)
    pool = mod.Pool(
        "fake",
        min_size=size,
        max_size=size,
        max_inflight_per_conn=8,
        connections=[micro3._conn(loop) for _ in range(size)],
    )
    kw: dict[str, Any] = {"into": micro3.P, "strategy": "direct"}
    sels = [pool.select_one_or_none] * tasks
    await micro3._round(sels, n // 4, kw)
    prof = cProfile.Profile()
    prof.enable()
    await micro3._round(sels, n, kw)
    prof.disable()
    pstats.Stats(prof).sort_stats("tottime").print_stats(18)


def main(*, tasks: int = 64, size: int = 16, n: int = 100_000, v: str = "") -> None:
    uvloop.run(_run(tasks, size, n, f"{micro3.VDIR}/{v}.py" if v else ""))
