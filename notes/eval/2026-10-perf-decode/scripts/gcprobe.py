#!yeet
"""Why does type_cost see no GC cost? Count collections for the uuid x8 tuple case."""

import gc
import time

import uvloop
from ab import DSN

import fastpg

SQL = "SELECT " + ", ".join(f"external_id AS c{i}" for i in range(8)) + " FROM people WHERE id BETWEEN $1 AND $2"
_n = [0, 0]


def _cb(phase: str, info: dict[str, int]) -> None:
    if phase == "start":
        _n[0] += 1
        _n[1] -= time.perf_counter_ns()
    else:
        _n[1] += time.perf_counter_ns()


async def _len(conn: fastpg.Connection, start: int) -> int:
    return len(await conn.select_many(SQL, start, start + 9_999))


async def _run(fetches: int) -> None:
    conn = await fastpg.connect(DSN)
    gc.callbacks.append(_cb)
    for mode in ("len", "held", "len", "held"):
        gc.collect()
        _n[:] = [0, 0]
        c0 = time.thread_time_ns()
        for i in range(fetches):
            start = 1 + i * 1000
            if mode == "len":
                await _len(conn, start)
            else:
                result = await conn.select_many(SQL, start, start + 9_999)
                del result
        cpu = time.thread_time_ns() - c0
        print(f"{mode:5}: {cpu / fetches / 1e4:.0f} ns/row, {_n[0]} collections, {_n[1] / 1e6:.1f} ms in GC")
    await conn.close()


def main(fetches: int = 20) -> None:
    uvloop.run(_run(fetches))
