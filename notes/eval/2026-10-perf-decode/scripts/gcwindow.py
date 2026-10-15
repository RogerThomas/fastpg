#!yeet
"""Does a collection triggered by a fetch run inside decode_cost.py's per-fetch window?"""

import gc
import time

import uvloop
from ab import DSN, SQL, Person

import fastpg

_starts: list[int] = []


def _cb(phase: str, info: dict[str, int]) -> None:
    if phase == "start":
        _starts.append(time.thread_time_ns())


async def _fetch_range(conn: fastpg.Connection, start: int, n: int) -> list[Person]:
    return await conn.select_many(SQL, start, start + n - 1, into=Person)


async def _run(fetches: int) -> None:
    conn = await fastpg.connect(DSN)
    gc.callbacks.append(_cb)
    gc.collect()
    _starts.clear()
    inside = outside = 0
    for i in range(fetches):
        start = 1 + (i * 10_000) % 90_000
        w0, c0 = time.perf_counter_ns(), time.thread_time_ns()
        result = await _fetch_range(conn, start, 10_000)
        wall = time.perf_counter_ns() - w0
        c1 = time.thread_time_ns()
        assert len(result) == 10_000
        del result, wall
        inside += sum(c0 <= t <= c1 for t in _starts)
        outside += sum(not (c0 <= t <= c1) for t in _starts)
        _starts.clear()
    print(f"collections inside the per-fetch CPU window: {inside}, outside: {outside}")
    await conn.close()


def main(fetches: int = 30) -> None:
    uvloop.run(_run(fetches))
