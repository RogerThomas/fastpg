#!yeet
"""Fetch N rows into Person (or tuples / pydantic) in a loop, for `sample` to profile."""

import datetime as dt
import decimal
import os
import sys
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


async def _run(rows: int, seconds: float, target: str) -> None:
    conn = await fastpg.connect(DSN)
    into: type[object] | None = {"struct": Person, "pydantic": PersonModel, "tuple": None}[target]
    print(f"pid {os.getpid()}", file=sys.stderr, flush=True)
    deadline = time.perf_counter() + seconds
    n = 0
    cpu0 = time.thread_time_ns()
    while time.perf_counter() < deadline:
        start = 1 + (n * rows) % (100_000 - rows)
        if into is None:
            result = await conn.select_many(SQL, start, start + rows - 1)
        else:
            result = await conn.select_many(SQL, start, start + rows - 1, into=into)
        assert len(result) == rows
        del result
        n += 1
    cpu = time.thread_time_ns() - cpu0
    print(f"{n} fetches, {cpu / (n * rows):.0f} ns/row loop CPU", file=sys.stderr)
    await conn.close()


def main(rows: int = 10_000, seconds: float = 10.0, target: str = "struct") -> None:
    uvloop.run(_run(rows, seconds, target))
