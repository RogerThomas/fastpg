#!yeet
"""Pool bookkeeping per statement, without the event loop: coroutines driven by hand.

Each statement's coroutine is sent None twice: the fake connection's awaitable yields once
(standing in for the wait on the socket), then returns. Pool vs the bare fake, interleaved.
"""

import asyncio
import gc
import importlib.util
import statistics
import sys
import time
from collections.abc import Generator
from typing import Any

import fastpg

SQL = "SELECT 1 WHERE $1"
VDIR = str(__import__("pathlib").Path(__file__).resolve().parent / "v")


def _load(path: str) -> Any:
    if not path:
        return fastpg
    spec = importlib.util.spec_from_file_location("fastpg._variant", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fastpg._variant"] = mod
    spec.loader.exec_module(mod)
    return mod


class _Once:
    __slots__ = ("value",)

    def __init__(self, value: object) -> None:
        self.value = value

    def __await__(self) -> Generator[None, None, object]:
        yield None
        return self.value


class FakeConn:
    closed = False

    async def select_one_or_none(self, query: str, *params: object, **kw: Any) -> object:
        return await _Once(params)

    async def close(self) -> None:
        pass


KW: dict[str, Any] = {}


def _drive_pool(pool: Any, n: int) -> float:
    sel = pool.select_one_or_none
    kw = KW
    c0 = time.thread_time_ns()
    for i in range(n):
        c = sel(SQL, i, **kw)
        c.send(None)
        try:
            c.send(None)
        except StopIteration:
            pass
    return (time.thread_time_ns() - c0) / n


def _drive_conn(conn: FakeConn, n: int) -> float:
    sel = conn.select_one_or_none
    kw = KW
    c0 = time.thread_time_ns()
    for i in range(n):
        c = sel(SQL, i, **kw)
        c.send(None)
        try:
            c.send(None)
        except StopIteration:
            pass
    return (time.thread_time_ns() - c0) / n


async def _run(size: int, n: int, rounds: int, variants: list[str]) -> None:
    pools = []
    for v in variants:
        conns: list[Any] = [FakeConn() for _ in range(size)]
        pools.append(
            _load(v).Pool("fake", min_size=size, max_size=size, max_inflight_per_conn=8, connections=conns)
        )
    bare = FakeConn()
    for p in pools:
        _drive_pool(p, n // 4)
    _drive_conn(bare, n // 4)
    res: dict[int, list[float]] = {i: [] for i in range(len(pools))}
    gc.disable()
    for _ in range(rounds):
        for i, p in enumerate(pools):
            b = _drive_conn(bare, n)
            a = _drive_pool(p, n)
            res[i].append(a - b)
    gc.enable()
    for i, v in enumerate(variants):
        d = res[i]
        print(f"{(v.rsplit('/', 1)[-1] or 'installed'):24s} overhead ns/op: med {statistics.median(d):6.0f}  min {min(d):6.0f}  max {max(d):6.0f}")


def main(*, v: list[str] | None = None, size: int = 16, n: int = 200_000, rounds: int = 15, kw: bool = False) -> None:
    if kw:
        KW.update(into=FakeConn, strategy="direct")
    asyncio.run(_run(size, n, rounds, [("" if x == "installed" else f"{VDIR}/{x}.py") for x in (v or ["installed"])]))
