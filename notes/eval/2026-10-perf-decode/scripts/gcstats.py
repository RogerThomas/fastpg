#!yeet
"""How many collections per generation, and how long, during repeated 10k-row fetches."""

import gc
import time
from collections import Counter

import uvloop
from ab import DSN, SQL, Person, PersonModel

import fastpg

_starts: list[int] = []
_events: Counter[int] = Counter()
_ns: Counter[int] = Counter()


def _cb(phase: str, info: dict[str, int]) -> None:
    if phase == "start":
        _starts.append(time.perf_counter_ns())
    else:
        _events[info["generation"]] += 1
        _ns[info["generation"]] += time.perf_counter_ns() - _starts.pop()


async def _run(fetches: int, target: str, rows: int, collect_first: bool) -> None:
    conn = await fastpg.connect(DSN)
    into = {"struct": Person, "pydantic": PersonModel}[target]
    if collect_first:
        gc.collect()
    print("tracked objects at start:", len(gc.get_objects()))
    gc.callbacks.append(_cb)
    t0 = time.thread_time_ns()
    for i in range(fetches):
        start = 1 + (i * rows) % (100_000 - rows)
        result = await conn.select_many(SQL, start, start + rows - 1, into=into)
        del result
    cpu = time.thread_time_ns() - t0
    gc.callbacks.remove(_cb)
    total = sum(_ns.values())
    print(f"{fetches} fetches: loop CPU {cpu / fetches / 1e3:.0f} us/fetch, "
          f"GC {total / fetches / 1e3:.0f} us/fetch ({total / cpu * 100:.0f}%)")
    for gen in sorted(_events):
        print(f"  gen{gen}: {_events[gen]} collections, {_ns[gen] / _events[gen] / 1e3:.0f} us each")
    await conn.close()


def main(fetches: int = 100, target: str = "struct", rows: int = 10_000, *, no_collect: bool = False) -> None:
    uvloop.run(_run(fetches, target, rows, not no_collect))
