#!yeet
"""Sequential single-row INSERT ... RETURNING for a while (for sampling profilers)."""

import datetime as dt
import decimal
import os
import time
import uuid

import uvloop

import fastpg


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


async def amain(seconds: float, returning: bool) -> None:
    conn = await fastpg.connect("postgresql://fastpg:fastpg@localhost:55432/fastpg")
    await conn.execute_script("CREATE TEMP TABLE w (LIKE people INCLUDING ALL)")
    sql = "INSERT INTO w VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)"
    rows = [make_row(i) for i in range(1, 200_001)]
    print("pid", os.getpid(), flush=True)
    end = time.perf_counter() + seconds
    n = 0
    p0, t0 = time.process_time(), time.perf_counter()
    while time.perf_counter() < end:
        if returning:
            await conn.select_one(sql + " RETURNING id", *rows[n])
        else:
            await conn.execute(sql, *rows[n])
        n += 1
    cpu, wall = time.process_time() - p0, time.perf_counter() - t0
    print(f"{n / wall:,.0f} req/s, {cpu * 1e6 / n:,.1f} us CPU/req", flush=True)
    await conn.close()


def main(seconds: float = 10.0, *, returning: bool = True) -> None:
    uvloop.run(amain(seconds, returning))
