#!yeet
"""Point lookups into a 10-column Struct: user and system CPU per query, per round.

Modes:

* ``struct`` / ``tuple`` / ``select1``: sequential ``select_one`` on one connection;
* ``pipe<T>``: ``T`` tasks, each running point lookups in a loop, on one connection;
* ``pool<N>x<T>``: ``T`` such tasks over ``create_pool(min_size=N, max_size=N)``;
* ``asyncpg``: sequential ``fetchrow`` + ``Person(*record)``, for reference.

Prints user / sys / loop-thread / wall µs per query (median, min, max over the rounds), or
with ``HOT_JSON=1`` one ``JSON [[user, sys, thread, wall], ...]`` line for ``ab.py``. With an
``alloc-stats`` build it also prints Rust allocations per query.

    uv run yeet ./notes/eval/2026-10-hot-path/hot.py --mode struct -n 5000 --rounds 7
"""

import asyncio
import datetime as dt
import decimal
import gc
import json
import os
import resource
import statistics
import time
import uuid
from typing import Any

import msgspec
import uvloop

import fastpg
from fastpg._native import rust_allocations

DSN = os.environ.get("FASTPG_DSN", "postgresql://fastpg:fastpg@127.0.0.1:55432/fastpg")
SQL = (
    "SELECT id, name, age, email, score, balance, active, external_id, created_at, birthday "
    "FROM people WHERE id = $1"
)


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


async def _worker(conn: Any, k: int, n: int, tasks: int) -> None:
    for i in range(k, n, tasks):
        await conn.select_one(SQL, i % 100_000 + 1, into=Person)


async def _queries(conn: Any, n: int, mode: str) -> None:
    if mode == "struct":
        for i in range(n):
            await conn.select_one(SQL, i % 100_000 + 1, into=Person)
    elif mode.startswith("pipe"):
        tasks = int(mode[4:] or 32)
        await asyncio.gather(*(_worker(conn, k, n, tasks) for k in range(tasks)))
    elif mode.startswith("pool"):
        tasks = int(mode.split("x")[1])
        await asyncio.gather(*(_worker(conn, k, n, tasks) for k in range(tasks)))
    elif mode == "tuple":
        for i in range(n):
            await conn.select_one(SQL, i % 100_000 + 1)
    elif mode == "asyncpg":
        for i in range(n):
            Person(*await conn.fetchrow(SQL, i % 100_000 + 1))
    else:
        for _ in range(n):
            await conn.select_one("SELECT 1")


async def _round(conn: Any, n: int, mode: str) -> list[float]:
    gc.collect()
    a0 = rust_allocations()
    r0 = resource.getrusage(resource.RUSAGE_SELF)
    t0, w0 = time.thread_time(), time.perf_counter()
    await _queries(conn, n, mode)
    w, t = time.perf_counter() - w0, time.thread_time() - t0
    r1 = resource.getrusage(resource.RUSAGE_SELF)
    a1 = rust_allocations()
    if a0 is not None and a1 is not None:
        print(f"rust allocations per query: {(a1 - a0) / n:.2f}")
    us = 1e6 / n
    return [(r1.ru_utime - r0.ru_utime) * us, (r1.ru_stime - r0.ru_stime) * us, t * us, w * us]


async def _connect(mode: str) -> Any:
    if mode == "asyncpg":
        import asyncpg

        conn = await asyncpg.connect(DSN)
        for i in range(500):
            await conn.fetchrow(SQL, i + 1)
        return conn
    if mode.startswith("pool"):
        size = int(mode[4:].split("x")[0])
        pool = await fastpg.create_pool(DSN, min_size=size, max_size=size)
        for i in range(500):
            await pool.select_one(SQL, i + 1, into=Person)
        return pool
    conn = await fastpg.connect(DSN)
    for i in range(500):
        await conn.select_one(SQL, i + 1, into=Person)
        await conn.select_one(SQL, i + 1)
        await conn.select_one("SELECT 1")
    return conn


async def _run(n: int, rounds: int, mode: str) -> None:
    conn = await _connect(mode)
    results = [await _round(conn, n, mode) for _ in range(rounds)]
    await conn.close()
    if os.environ.get("HOT_JSON"):
        print("JSON " + json.dumps(results))
        return
    for label, idx in (("user", 0), ("sys", 1), ("thread", 2), ("wall", 3)):
        vals = sorted(r[idx] for r in results)
        print(
            f"{label:>6} µs/req: median {statistics.median(vals):6.2f}"
            f"  min {vals[0]:6.2f}  max {vals[-1]:6.2f}",
        )


def main(*, n: int = 5000, rounds: int = 7, mode: str = "struct", loop: str = "uvloop") -> None:
    if loop == "uvloop":
        uvloop.run(_run(n, rounds, mode))
    else:
        asyncio.run(_run(n, rounds, mode))
