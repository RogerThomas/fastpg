#!yeet
"""Rust heap allocations per row for each bulk path (needs the `alloc-stats` build)."""

import datetime as dt
import decimal
import uuid

import msgspec
import uvloop

import fastpg
from fastpg._native import rust_allocations


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


async def amain(rows: int) -> None:
    conn = await fastpg.connect("postgresql://fastpg:fastpg@localhost:55432/fastpg")
    await conn.execute_script("CREATE TEMP TABLE w (LIKE people INCLUDING ALL)")
    tuples = [make_row(i) for i in range(1, rows + 1)]
    people = [Person(*t) for t in tuples]
    sql = "INSERT INTO w VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)"
    methods = {
        "execute_many (tuples)": lambda: conn.execute_many(sql, tuples),
        "insert_many (Structs)": lambda: conn.insert_many("w", people),
        "copy_in (Structs)": lambda: conn.copy_in("w", people),
        "copy_in (tuples)": lambda: conn.copy_in("w", tuples),
    }
    for name, run in methods.items():
        await conn.execute_script("TRUNCATE w")
        await run()  # warm-up
        await conn.execute_script("TRUNCATE w")
        before = rust_allocations()
        await run()
        after = rust_allocations()
        assert before is not None and after is not None
        print(f"{name:24} {(after - before) / rows:6.2f} Rust allocations/row")
    await conn.close()


def main(rows: int = 100_000) -> None:
    uvloop.run(amain(rows))
