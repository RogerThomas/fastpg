#!yeet
"""Loop-thread CPU per point lookup against the real database, interleaved rounds.

Each round runs the same N lookups (TASKS tasks back to back) on: pinned connections (task k
owns connection k), then a pool of SIZE per variant (shared mode). Reports per-round CPU µs
per lookup (time.thread_time) and the pool's extra CPU over pinned, median and range.
"""

import asyncio
import gc
import importlib.util
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import uvloop

import fastpg

sys.path.insert(0, str(Path(__file__).resolve().parent))
from person import Person  # noqa: E402

SQL_ONE = (
    "SELECT id, name, age, email, score, balance, active, external_id, created_at, birthday "
    "FROM people WHERE id = $1"
)
DSN = "postgresql://fastpg:fastpg@localhost:55432/fastpg?sslmode=disable"
VDIR = Path(__file__).resolve().parent / "v"


def _load(name: str) -> Any:
    if name == "installed":
        return sys.modules["fastpg._pool"]
    spec = importlib.util.spec_from_file_location(f"fastpg._variant_{name}", VDIR / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


async def _worker(sel: Any, k: int, n: int, tasks: int) -> None:
    for i in range(k, n, tasks):
        await sel(SQL_ONE, i % 100_000 + 1, into=Person, strategy="direct")


async def _round(sels: list[Any], n: int) -> tuple[float, float]:
    tasks = len(sels)
    gc.collect()
    c0, w0 = time.thread_time(), time.perf_counter()
    await asyncio.gather(*(_worker(sels[k], k, n, tasks) for k in range(tasks)))
    return (time.thread_time() - c0) * 1e6 / n, n / (time.perf_counter() - w0)


async def _run(tasks: int, size: int, n: int, rounds: int, variants: list[str]) -> None:
    pools = []
    for name in variants:
        conns = [await fastpg.connect(DSN) for _ in range(size)]
        pools.append(
            _load(name).Pool(DSN, min_size=size, max_size=size, max_inflight_per_conn=8, connections=conns)
        )
    pinned = [await fastpg.connect(DSN) for _ in range(tasks)]
    pin_sels = [c.select_one_or_none for c in pinned]
    pool_sels = [[p.select_one_or_none] * tasks for p in pools]
    await _round(pin_sels, n // 4)
    for sels in pool_sels:
        await _round(sels, n // 4)
    pin_cpu: list[float] = []
    pin_ops: list[float] = []
    cpu: dict[str, list[float]] = {v: [] for v in variants}
    ops: dict[str, list[float]] = {v: [] for v in variants}
    diff: dict[str, list[float]] = {v: [] for v in variants}
    for _ in range(rounds):
        c0, o0 = await _round(pin_sels, n)
        pin_cpu.append(c0)
        pin_ops.append(o0)
        for name, sels in zip(variants, pool_sels, strict=True):
            c, o = await _round(sels, n)
            cpu[name].append(c)
            ops[name].append(o)
            diff[name].append(c - c0)
    med = statistics.median
    print(f"tasks={tasks} size={size} n={n} rounds={rounds}")
    print(f"  pinned      cpu µs/op med {med(pin_cpu):6.2f} [{min(pin_cpu):.2f}..{max(pin_cpu):.2f}]  ops/s med {med(pin_ops):,.0f}")
    for name in variants:
        d = diff[name]
        print(
            f"  {name:11s} cpu µs/op med {med(cpu[name]):6.2f} [{min(cpu[name]):.2f}..{max(cpu[name]):.2f}]"
            f"  ops/s med {med(ops[name]):,.0f}   pool-pinned med {med(d):5.2f} [{min(d):.2f}..{max(d):.2f}]"
        )
    for p in pools:
        await p.close()
    for c in pinned:
        await c.close()


def main(*, v: list[str] | None = None, tasks: int = 16, size: int = 0, n: int = 30_000, rounds: int = 9) -> None:
    uvloop.run(_run(tasks, size or tasks, n, rounds, v or ["installed"]))
