#!yeet
"""Pool bookkeeping on the event loop, through the real Connection code (fake native).

Each fake native answers a fetch on the next loop iteration. TASKS tasks run statements
back to back, either through a pool of SIZE connections or with task k fixed on
connection k % SIZE (no routing at all). With TASKS > SIZE the pool pipelines, so this
also covers the scoring path (rule 3), not just the idle stack.
"""

import asyncio
import gc
import importlib.util
import statistics
import sys
import time
from typing import Any

import msgspec
import uvloop

import fastpg

SQL = "SELECT 1 WHERE $1"
VDIR = str(__import__("pathlib").Path(__file__).resolve().parent / "v")
ROW = (1,)


class P(msgspec.Struct):
    id: int


def _load(path: str) -> Any:
    if not path:
        return sys.modules["fastpg._pool"]
    spec = importlib.util.spec_from_file_location("fastpg._variant", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fastpg._variant"] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeNative:
    closed = False

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._call_soon = loop.call_soon

    def fetch(self, future: Any, query: str, params: object, shape: int, spec: object = None) -> None:
        self._call_soon(future.set_result, ROW)

    def close(self) -> None:
        pass

    def fileno(self) -> int:
        return -1


def _conn(loop: asyncio.AbstractEventLoop) -> fastpg.Connection:
    c = fastpg.Connection(FakeNative(loop), loop, loop.create_future())  # type: ignore[arg-type]
    object.__setattr__(c, "_released", True)
    return c


async def _w(sel: Any, k: int, n: int, tasks: int, kw: dict[str, Any]) -> None:
    for i in range(k, n, tasks):
        await sel(SQL, i, **kw)


async def _round(sels: list[Any], n: int, kw: dict[str, Any]) -> float:
    tasks = len(sels)
    gc.collect()
    c0 = time.thread_time_ns()
    await asyncio.gather(*(_w(sels[k], k, n, tasks, kw) for k in range(tasks)))
    return (time.thread_time_ns() - c0) / n


async def _run(tasks: int, size: int, n: int, rounds: int, variants: list[str], kw: dict[str, Any], cap: int) -> None:
    loop = asyncio.get_running_loop()
    pools = []
    for v in variants:
        pools.append(
            _load(v).Pool(
                "fake",
                min_size=size,
                max_size=size,
                max_inflight_per_conn=cap,
                connections=[_conn(loop) for _ in range(size)],
            )
        )
    conns = [_conn(loop) for _ in range(size)]
    fixed = [conns[k % size].select_one_or_none for k in range(tasks)]
    for p in pools:
        await _round([p.select_one_or_none] * tasks, n // 4, kw)
    await _round(fixed, n // 4, kw)
    res: dict[int, list[float]] = {i: [] for i in range(len(pools))}
    base: list[float] = []
    for _ in range(rounds):
        for i, p in enumerate(pools):
            b = await _round(fixed, n, kw)
            a = await _round([p.select_one_or_none] * tasks, n, kw)
            base.append(b)
            res[i].append(a - b)
    print(f"tasks={tasks} size={size} cap={cap}: fixed assignment ns/op med {statistics.median(base):6.0f}")
    for i, v in enumerate(variants):
        d = res[i]
        print(f"  {(v.rsplit('/', 1)[-1] or 'installed'):22s} pool overhead ns/op: med {statistics.median(d):6.0f}  min {min(d):6.0f}  max {max(d):6.0f}")


def main(
    *,
    v: list[str] | None = None,
    tasks: int = 16,
    size: int = 16,
    n: int = 200_000,
    rounds: int = 9,
    kw: bool = True,
    cap: int = 8,
) -> None:
    kwargs: dict[str, Any] = {"into": P, "strategy": "direct"} if kw else {}
    variants = [("" if x == "installed" else f"{VDIR}/{x}.py") for x in (v or ["installed"])]
    uvloop.run(_run(tasks, size, n, rounds, variants, kwargs, cap))
