#!yeet
"""Server (backend process) CPU per row for each write path, next to the client's loop CPU.

Reads the backend's utime+stime from /proc inside the compose container (read-only
`docker exec cat`), before and after one write of `rows` people-shaped rows into a TEMP table.
"""

import datetime as dt
import decimal
import os
import statistics
import subprocess
import time
import uuid

import asyncpg
import msgspec
import uvloop

import fastpg

TICK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
COLUMNS = "id, name, age, email, score, balance, active, external_id, created_at, birthday"
TYPES = ["int4", "text", "int4", "text", "float8", "numeric", "bool", "uuid", "timestamptz", "date"]
UNNEST = (
    f"INSERT INTO w ({COLUMNS}) SELECT * FROM unnest("
    + ", ".join(f"${i + 1}::{t}[]" for i, t in enumerate(TYPES))
    + ")"
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


def backend_ticks(pid: int) -> int:
    out = subprocess.run(
        ["docker", "exec", "fastpg-postgres-1", "cat", f"/proc/{pid}/stat"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    fields = out.rsplit(")", 1)[1].split()
    return int(fields[11]) + int(fields[12])  # utime, stime (fields 14, 15 of stat)


async def amain(rows: int, repeat: int) -> None:
    dsn = "postgresql://fastpg:fastpg@localhost:55432/fastpg"
    tuples = [make_row(i) for i in range(1, rows + 1)]
    people = [Person(*t) for t in tuples]
    fconn = await fastpg.connect(dsn)
    aconn = await asyncpg.connect(dsn)
    await fconn.execute_script("CREATE TEMP TABLE w (LIKE people INCLUDING ALL)")
    await aconn.execute("CREATE TEMP TABLE w (LIKE people INCLUDING ALL)")
    (fpid,) = await fconn.select_one("SELECT pg_backend_pid()")
    apid = await aconn.fetchval("SELECT pg_backend_pid()")
    sql = "INSERT INTO w VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)"

    async def unnest() -> None:
        async with aconn.transaction():
            batches = [
                [list(col) for col in zip(*tuples[i : i + 1024], strict=True)]
                for i in range(0, rows, 1024)
            ]
            await aconn.executemany(UNNEST, batches)

    methods = {
        "fastpg execute_many": (fpid, fconn.execute_script, lambda: fconn.execute_many(sql, tuples)),
        "fastpg insert_many": (fpid, fconn.execute_script, lambda: fconn.insert_many("w", people)),
        "fastpg copy_in": (fpid, fconn.execute_script, lambda: fconn.copy_in("w", people)),
        "asyncpg executemany": (apid, aconn.execute, lambda: aconn.executemany(sql, tuples)),
        "asyncpg unnest x1024": (apid, aconn.execute, unnest),
        "asyncpg copy": (
            apid,
            aconn.execute,
            lambda: aconn.copy_records_to_table("w", records=tuples, columns=COLUMNS.split(", ")),
        ),
    }
    results: dict[str, list[tuple[float, float, float]]] = {k: [] for k in methods}
    for r in range(repeat + 1):
        for name, (pid, script, run) in methods.items():
            await script("TRUNCATE w")
            t0 = backend_ticks(pid)
            w0, c0 = time.perf_counter(), time.thread_time()
            await run()
            wall, cpu = time.perf_counter() - w0, time.thread_time() - c0
            server = (backend_ticks(pid) - t0) / TICK
            if r:
                results[name].append((rows / wall, cpu * 1e9 / rows, server * 1e9 / rows))
    for name, runs in results.items():
        print(
            f"{name:22} {statistics.median(x for x, _, _ in runs):>11,.0f} rows/s"
            f"  client {statistics.median(y for _, y, _ in runs):>6,.0f} ns/row"
            f"  server {statistics.median(z for _, _, z in runs):>6,.0f} ns/row"
        )
    await fconn.close()
    await aconn.close()


def main(rows: int = 102_400, repeat: int = 5) -> None:
    uvloop.run(amain(rows, repeat))
