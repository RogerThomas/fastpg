#!yeet
"""Chunk size / in-flight / insert group sweeps for the bulk writes (BulkSettings patched).

Rounds are interleaved across settings; median rows/s and loop ns/row per setting.
"""

import datetime as dt
import decimal
import gc
import statistics
import time
import uuid

import msgspec
import uvloop

import fastpg
from fastpg._writes import BulkSettings


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


def apply(setting: str) -> None:
    for part in setting.split(","):
        key, value = part.split("=")
        setattr(BulkSettings, key, int(value))


async def amain(method: str, settings: list[str], rows: int, repeat: int) -> None:
    dsn = "postgresql://fastpg:fastpg@localhost:55432/fastpg"
    tuples = [make_row(i) for i in range(1, rows + 1)]
    people = [Person(*t) for t in tuples]
    defaults = {k: getattr(BulkSettings, k) for k in ("execute_chunk", "copy_chunk", "in_flight", "insert_group")}
    conn = await fastpg.connect(dsn)
    await conn.execute_script("CREATE TEMP TABLE w (LIKE people INCLUDING ALL)")
    sql = "INSERT INTO w VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)"
    results: dict[str, list[tuple[float, float]]] = {s: [] for s in settings}
    for r in range(repeat + 1):
        for setting in settings:
            for k, v in defaults.items():
                setattr(BulkSettings, k, v)
            apply(setting)
            await conn.execute_script("TRUNCATE w")
            gc.collect()
            w0, c0 = time.perf_counter(), time.thread_time()
            if method == "execute_many":
                await conn.execute_many(sql, tuples)
            elif method == "insert_many":
                await conn.insert_many("w", people)
            else:
                await conn.copy_in("w", people)
            wall, cpu = time.perf_counter() - w0, time.thread_time() - c0
            if r:
                results[setting].append((rows / wall, cpu * 1e9 / rows))
    for setting, runs in results.items():
        rates = sorted(x for x, _ in runs)
        cpu = statistics.median(y for _, y in runs)
        print(f"{method:13} {setting:40} {statistics.median(rates):>11,.0f} rows/s"
              f"  ({rates[0]:,.0f}-{rates[-1]:,.0f})  {cpu:>6,.0f} ns/row")
    await conn.close()


def main(method: str, settings: str, rows: int = 100_000, repeat: int = 5) -> None:
    uvloop.run(amain(method, settings.split(";"), rows, repeat))
