#!yeet
"""Pool bookkeeping per statement, hand-driven, through the real Connection query path.

Each Connection wraps a fake native object whose ``fetch`` does nothing; the driver loop
resolves the future the coroutine yields, then resumes it. Pool vs a bare Connection.
"""

import asyncio
import gc
import importlib.util
import statistics
import sys
import time
from typing import Any

import msgspec

import fastpg

SQL = "SELECT 1 WHERE $1"
VDIR = str(__import__("pathlib").Path(__file__).resolve().parent / "v")


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

    def fetch(self, future: object, query: str, params: object, shape: int, spec: object = None) -> None:
        pass

    def close(self) -> None:
        pass


def _conn(loop: asyncio.AbstractEventLoop) -> fastpg.Connection:
    c = fastpg.Connection(FakeNative(), loop, loop.create_future())  # type: ignore[arg-type]
    return c


KW: dict[str, Any] = {}
ROW = (1,)


def _drive(sel: Any, n: int) -> float:
    kw = KW
    row = ROW
    c0 = time.thread_time_ns()
    for i in range(n):
        c = sel(SQL, i, **kw)
        fut = c.send(None)
        fut.set_result(row)
        try:
            c.send(None)
        except StopIteration:
            pass
    return (time.thread_time_ns() - c0) / n


async def _run(size: int, n: int, rounds: int, variants: list[str]) -> None:
    loop = asyncio.get_running_loop()
    pools = []
    for v in variants:
        conns = [_conn(loop) for _ in range(size)]
        pools.append(
            _load(v).Pool("fake", min_size=size, max_size=size, max_inflight_per_conn=8, connections=conns)
        )
    bare = _conn(loop)
    for p in pools:
        _drive(p.select_one_or_none, n // 4)
    _drive(bare.select_one_or_none, n // 4)
    res: dict[int, list[float]] = {i: [] for i in range(len(pools))}
    base: list[float] = []
    gc.disable()
    for _ in range(rounds):
        for i, p in enumerate(pools):
            b = _drive(bare.select_one_or_none, n)
            a = _drive(p.select_one_or_none, n)
            base.append(b)
            res[i].append(a - b)
    gc.enable()
    print(f"bare connection ns/op: med {statistics.median(base):6.0f}")
    for i, v in enumerate(variants):
        d = res[i]
        print(f"{(v.rsplit('/', 1)[-1] or 'installed'):24s} overhead ns/op: med {statistics.median(d):6.0f}  min {min(d):6.0f}  max {max(d):6.0f}")


def main(*, v: list[str] | None = None, size: int = 16, n: int = 200_000, rounds: int = 15, kw: bool = False) -> None:
    if kw:
        KW.update(into=P, strategy="direct")
    asyncio.run(_run(size, n, rounds, [("" if x == "installed" else f"{VDIR}/{x}.py") for x in (v or ["installed"])]))
