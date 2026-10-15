#!yeet
"""Python-side cost of ``Connection.select_one(..., into=Person)``, with a stub native connection.

The stub resolves the future inside ``fetch``, so the ``await`` doesn't suspend: this times the
coroutine layering, ``create_future``, the plan lookup and the call into "native" (a Python
method here). No database needed. ``pyab.py`` compares builds.

    uv run yeet ./notes/eval/2026-10-hot-path/pyover.py
"""

import asyncio
import datetime as dt
import decimal
import statistics
import time
import uuid

import msgspec
import uvloop

from fastpg._connection import Connection


class Person(msgspec.Struct):
    id: int
    name: str
    age: int
    email: str | None
    score: float
    balance: decimal.Decimal
    active: bool
    external_id: uuid.UUID
    created_at: dt.datetime
    birthday: dt.date | None


class Stub:
    def fetch(
        self,
        future: asyncio.Future[object],
        sql: str,
        params: object,
        shape: int,
        spec: object = None,
    ) -> None:
        future.set_result(None)


async def _bench(n: int, rounds: int) -> None:
    loop = asyncio.get_running_loop()
    conn = Connection(Stub(), loop, loop.create_future())  # type: ignore[arg-type]
    conn._released = True  # nothing to unregister
    for _ in range(1000):
        await conn.select_one("SELECT $1", 1, into=Person)
    res = []
    for _ in range(rounds):
        t0 = time.perf_counter_ns()
        for _ in range(n):
            await conn.select_one("SELECT $1", 1, into=Person)
        res.append((time.perf_counter_ns() - t0) / n)
    res.sort()
    print(f"select_one python overhead: min {res[0]:.0f} ns  median {statistics.median(res):.0f} ns")


def main(*, n: int = 200_000, rounds: int = 9) -> None:
    uvloop.run(_bench(n, rounds))
