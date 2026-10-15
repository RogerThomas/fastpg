#!yeet
"""Server-side cost of INSERT shapes, measured with asyncpg as the client.

multi-row VALUES (64 rows/statement) vs INSERT ... SELECT FROM unnest(arrays) (one statement
per 1024 rows) vs COPY, into a TEMP people-shaped table. Interleaved rounds, medians.
"""

import datetime as dt
import decimal
import statistics
import time
import uuid

import asyncpg
import uvloop

COLUMNS = "id, name, age, email, score, balance, active, external_id, created_at, birthday"
TYPES = ["int4", "text", "int4", "text", "float8", "numeric", "bool", "uuid", "timestamptz", "date"]


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


def values_sql(group: int) -> str:
    tuples = ", ".join(
        "(" + ", ".join(f"${r * 10 + c + 1}" for c in range(10)) + ")" for r in range(group)
    )
    return f"INSERT INTO w ({COLUMNS}) VALUES {tuples}"


UNNEST = (
    f"INSERT INTO w ({COLUMNS}) SELECT * FROM unnest("
    + ", ".join(f"${i + 1}::{t}[]" for i, t in enumerate(TYPES))
    + ")"
)


async def values(conn: asyncpg.Connection, rows: list[tuple[object, ...]], group: int) -> None:
    sql = values_sql(group)
    args = [[v for row in rows[i : i + group] for v in row] for i in range(0, len(rows), group)]
    async with conn.transaction():
        await conn.executemany(sql, args)


async def unnest(conn: asyncpg.Connection, rows: list[tuple[object, ...]], chunk: int) -> None:
    async with conn.transaction():
        batches = []
        for i in range(0, len(rows), chunk):
            part = rows[i : i + chunk]
            batches.append([list(col) for col in zip(*part, strict=True)])
        await conn.executemany(UNNEST, batches)


async def copy(conn: asyncpg.Connection, rows: list[tuple[object, ...]]) -> None:
    await conn.copy_records_to_table("w", records=rows, columns=COLUMNS.split(", "))


async def amain(rows: int, repeat: int) -> None:
    data = [make_row(i) for i in range(1, rows + 1)]
    conn = await asyncpg.connect("postgresql://fastpg:fastpg@localhost:55432/fastpg")
    await conn.execute("CREATE TEMP TABLE w (LIKE people INCLUDING ALL)")
    shapes = {
        "VALUES x64": lambda: values(conn, data, 64),
        "unnest x1024": lambda: unnest(conn, data, 1024),
        "unnest x4096": lambda: unnest(conn, data, 4096),
        "COPY": lambda: copy(conn, data),
    }
    results: dict[str, list[tuple[float, float]]] = {k: [] for k in shapes}
    for r in range(repeat + 1):
        for name, run in shapes.items():
            await conn.execute("TRUNCATE w")
            w0, c0 = time.perf_counter(), time.thread_time()
            await run()
            wall, cpu = time.perf_counter() - w0, time.thread_time() - c0
            if r:
                results[name].append((rows / wall, cpu * 1e9 / rows))
    for name, runs in results.items():
        rates = sorted(x for x, _ in runs)
        print(
            f"asyncpg {name:14} {statistics.median(rates):>11,.0f} rows/s "
            f"({rates[0]:,.0f}-{rates[-1]:,.0f})  client {statistics.median(y for _, y in runs):,.0f} ns/row"
        )
    await conn.close()


def main(rows: int = 100_000, repeat: int = 5) -> None:
    uvloop.run(amain(rows, repeat))
