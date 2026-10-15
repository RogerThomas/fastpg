#!yeet
"""One measurement sample: loop-thread CPU per row and wall rows/s for a range fetch.

Same method as bench/decode_cost.py (thread_time around each awaited fetch, results
released inside the loop), printed as one JSON line so runs can be aggregated.
"""

import datetime as dt
import decimal
import gc
import json
import time
import uuid

import msgspec
import pydantic
import uvloop

import fastpg

DSN = "postgresql://fastpg:fastpg@127.0.0.1:55432/fastpg"
SQL = (
    "SELECT id, name, age, email, score, balance, active, external_id, created_at, birthday "
    "FROM people WHERE id BETWEEN $1 AND $2"
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


class PersonModel(pydantic.BaseModel):
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


_GC = [0, 0]


def _gc_cb(phase: str, info: dict[str, int]) -> None:
    if phase == "start":
        _GC[0] += 1
        _GC[1] -= time.thread_time_ns()
    else:
        _GC[1] += time.thread_time_ns()


async def _fetch(conn: fastpg.Connection, start: int, rows: int, target: str) -> list[object]:
    if target == "tuple":
        return await conn.select_many(SQL, start, start + rows - 1)
    if target == "struct":
        return await conn.select_many(SQL, start, start + rows - 1, into=Person)
    return await conn.select_many(SQL, start, start + rows - 1, into=PersonModel)


async def _run(label: str, rows: int, target: str, budget: int, sql: str) -> None:
    global SQL  # noqa: PLW0603 - scratch script
    if sql:
        SQL = sql
    conn = await fastpg.connect(DSN)
    for i in range(3):
        await _fetch(conn, 1 + i * rows, rows, target)
    iterations = max(5, budget // rows)
    gc.collect()
    gc.callbacks.append(_gc_cb)
    w0 = time.perf_counter_ns()
    c0 = time.thread_time_ns()
    for i in range(iterations):
        start = 1 + (i * rows) % (100_000 - rows)
        result = await _fetch(conn, start, rows, target)
        assert len(result) == rows
        del result
    cpu = time.thread_time_ns() - c0
    wall = time.perf_counter_ns() - w0
    gc.callbacks.remove(_gc_cb)
    await conn.close()
    out = {
        "label": label,
        "target": target,
        "rows": rows,
        "cpu_ns_row": cpu / (iterations * rows),
        "wall_rows_s": iterations * rows / (wall / 1e9),
        "gc_ns_row": _GC[1] / (iterations * rows),
        "collections": _GC[0],
    }
    print(json.dumps(out), flush=True)


def main(label: str, rows: int, target: str, *, budget: int = 300_000, sql: str = "") -> None:
    uvloop.run(_run(label, rows, target, budget, sql))
