#!yeet
"""Pool vs single connection: interleaved calls, and the original block layout repeated.

Reads the DSN from $NEON_DSN (never printed) unless --local is given.
"""

import os
import random
import statistics
import time
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any

import fastpg
from gap import LOCAL_DSN, Person, Result


async def _time(op: Callable[[], Awaitable[object]]) -> float:
    t0 = time.perf_counter()
    await op()
    return (time.perf_counter() - t0) * 1000


def _summary(label: str, ms: list[float]) -> str:
    q = statistics.quantiles(ms, n=4)
    return (
        f"{label:<22} n={len(ms):3d} median {statistics.median(ms):6.2f}  mean {statistics.fmean(ms):6.2f}"
        f"  p25 {q[0]:6.2f}  p75 {q[2]:6.2f}  min {min(ms):6.2f}"
    )


def _setup(local: bool) -> tuple[str, str, type[Any]]:
    if local:
        return LOCAL_DSN, "SELECT * FROM people ORDER BY id LIMIT 10000", Person
    dsn = os.environ["NEON_DSN"].strip()
    return dsn.replace("&channel_binding=require", ""), "SELECT * FROM results ORDER BY id", Result


async def _median_ms(op: Callable[[], Awaitable[object]], runs: int) -> float:
    await op()
    return statistics.median([await _time(op) for _ in range(runs)])


async def _conn_block(dsn: str, query: str, into: type[Any], runs: int) -> float:
    async with await fastpg.connect(dsn) as conn:
        for _ in range(10):
            await conn.select_one("SELECT 1")
        return await _median_ms(partial(conn.select_many, query, into=into), runs)


async def _pool_block(dsn: str, query: str, into: type[Any], runs: int, size: int) -> float:
    async with await fastpg.create_pool(dsn, min_size=size, max_size=size) as pool:
        return await _median_ms(partial(pool.select_many, query, into=into), runs)


async def blocks(*, local: bool = False, reps: int = 8, runs: int = 9) -> None:
    """The original script's layout (connect + median of 9, then pool + median of 9), repeated."""
    dsn, query, into = _setup(local)
    conn_m: list[float] = []
    pool_m: list[float] = []
    for rep in range(reps):
        if rep % 2 == 0:
            c = await _conn_block(dsn, query, into, runs)
            p = await _pool_block(dsn, query, into, runs, 4)
        else:
            p = await _pool_block(dsn, query, into, runs, 4)
            c = await _conn_block(dsn, query, into, runs)
        conn_m.append(c)
        pool_m.append(p)
        print(f"rep {rep} ({'conn first' if rep % 2 == 0 else 'pool first'}): conn {c:6.2f}  pool {p:6.2f}  pool-conn {p - c:+6.2f} ms")
    print(_summary("conn block medians", conn_m))
    print(_summary("pool block medians", pool_m))
    diffs = [p - c for p, c in zip(pool_m, conn_m, strict=True)]
    print(f"pool-conn per rep: median {statistics.median(diffs):+.2f}, range {min(diffs):+.2f}..{max(diffs):+.2f} ms")


async def _pinged(dsn: str, pings: int) -> fastpg.Connection:
    conn = await fastpg.connect(dsn)
    for _ in range(pings):
        await conn.select_one("SELECT 1")
    return conn


async def history(*, local: bool = False, iters: int = 200, seed: int = 1) -> None:
    """Interleaved: which connection history makes a plain connection slower?"""
    from gap import Tracer, tcp_info

    tracer = Tracer()
    tracer.install()
    dsn, query, into = _setup(local)
    rng = random.Random(seed)
    conns: dict[str, fastpg.Connection] = {}
    conns["first, +10 ping"] = await tracer.connect(dsn)
    for _ in range(10):
        await conns["first, +10 ping"].select_one("SELECT 1")
    conns["second, plain"] = await tracer.connect(dsn)
    conns["third, +10 ping"] = await tracer.connect(dsn)
    for _ in range(10):
        await conns["third, +10 ping"].select_one("SELECT 1")
    conns["fourth, plain"] = await tracer.connect(dsn)
    for conn in conns.values():
        await conn.select_many(query, into=into)
    times: dict[str, list[float]] = {k: [] for k in conns}
    order = list(conns)
    for _ in range(iters):
        rng.shuffle(order)
        for name in order:
            times[name].append(await _time(partial(conns[name].select_many, query, into=into)))
    for name, ms in times.items():
        info = tcp_info(tracer.sock_of(conns[name]))
        print(_summary(name, ms) + f"  rcvbuf {info.rcvbuf // 1024} KB")
        await conns[name].close()


async def main(*, local: bool = False, iters: int = 60, seed: int = 1) -> None:
    """Interleaved: one call per target per iteration, in shuffled order."""
    dsn, query, into = _setup(local)
    rng = random.Random(seed)
    conn = await fastpg.connect(dsn)
    for _ in range(10):
        await conn.select_one("SELECT 1")
    fresh = await fastpg.connect(dsn)
    pool4 = await fastpg.create_pool(dsn, min_size=4, max_size=4)
    pool1 = await fastpg.create_pool(dsn, min_size=1, max_size=1)
    targets: dict[str, Callable[[], Awaitable[object]]] = {
        "conn": partial(conn.select_many, query, into=into),
        "fresh conn": partial(fresh.select_many, query, into=into),
        "pool 4/4": partial(pool4.select_many, query, into=into),
        "pool 1/1": partial(pool1.select_many, query, into=into),
    }
    for op in targets.values():
        await op()  # warm up (prepare)
    times: dict[str, list[float]] = {k: [] for k in targets}
    order = list(targets)
    for _ in range(iters):
        rng.shuffle(order)
        for name in order:
            times[name].append(await _time(targets[name]))
    for name, ms in times.items():
        print(_summary(name, ms))
    for name in ("fresh conn", "pool 4/4", "pool 1/1"):
        d = [a - b for a, b in zip(times[name], times["conn"], strict=True)]
        wins = sum(1 for x in d if x > 0)
        print(f"{name + ' - conn':<22} paired median {statistics.median(d):+6.2f} ms, slower in {wins}/{len(d)}")
    print(f"pool 4/4 stats: {pool4.stats()}")
    await pool4.close()
    await pool1.close()
    await fresh.close()
    await conn.close()
