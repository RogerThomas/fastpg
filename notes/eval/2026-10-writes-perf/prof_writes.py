#!yeet
"""Loop one bulk-write method for a while (for sampling profilers)."""

import datetime as dt
import decimal
import os
import time
import uuid

import msgspec
import uvloop

import fastpg


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


def make_row(i: int) -> tuple[object, ...]:
    return (
        i,
        f"person-{i}",
        18 + i % 60,
        None if i % 7 == 0 else f"person{i}@example.com",
        (i % 1000) / 10,
        decimal.Decimal((i * 37) % 100_000) / 100,
        i % 2 == 0,
        uuid.UUID(int=i),
        dt.datetime(2024, 1, 1, tzinfo=dt.UTC) + dt.timedelta(seconds=i * 61),
        None if i % 11 == 0 else dt.date(1950, 1, 1) + dt.timedelta(days=i % 20_000),
    )


async def amain(method: str, rows: int, seconds: float) -> None:
    dsn = "postgresql://fastpg:fastpg@localhost:55432/fastpg"
    tuples = [make_row(i) for i in range(1, rows + 1)]
    people = [Person(*t) for t in tuples]
    conn = await fastpg.connect(dsn)
    await conn.execute_script("CREATE TEMP TABLE w (LIKE people INCLUDING ALL)")
    print("pid", os.getpid(), flush=True)
    end = time.perf_counter() + seconds
    n = 0
    t0 = time.perf_counter()
    c0 = time.thread_time()
    while time.perf_counter() < end:
        await conn.execute_script("TRUNCATE w")
        if method == "execute_many":
            sql = "INSERT INTO w VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)"
            await conn.execute_many(sql, tuples)
        elif method == "insert_many":
            await conn.insert_many("w", people)
        elif method == "insert_many_tuples":
            await conn.insert_many("w", tuples)
        elif method == "copy_in":
            await conn.copy_in("w", people)
        elif method == "copy_in_tuples":
            await conn.copy_in("w", tuples)
        n += rows
    wall = time.perf_counter() - t0
    cpu = time.thread_time() - c0
    print(f"{method}: {n / wall:,.0f} rows/s, {cpu * 1e9 / n:,.0f} ns/row loop CPU", flush=True)
    await conn.close()


def main(method: str = "execute_many", rows: int = 100_000, seconds: float = 10.0) -> None:
    uvloop.run(amain(method, rows, seconds))
