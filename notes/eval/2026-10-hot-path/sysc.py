#!yeet
"""Socket and poll syscalls per point lookup (macOS), counted by the syscount.c interposer.

    clang -dynamiclib -O2 -o /tmp/libsyscount.dylib notes/eval/2026-10-hot-path/syscount.c
    DYLD_INSERT_LIBRARIES=/tmp/libsyscount.dylib .venv/bin/yeet \\
        ./notes/eval/2026-10-hot-path/sysc.py [--tasks 32] [--driver asyncpg] [--loop asyncio]

(Run the venv's ``yeet`` directly: ``uv run`` doesn't pass ``DYLD_*`` through.) libc's
``recv`` / ``send`` call ``recvfrom`` / ``sendto``, so each is counted under both names.
"""

import asyncio
import ctypes
import datetime as dt
import decimal
import os
import uuid

import msgspec
import uvloop

import fastpg

DSN = os.environ.get("FASTPG_DSN", "postgresql://fastpg:fastpg@127.0.0.1:55432/fastpg")
SQL = (
    "SELECT id, name, age, email, score, balance, active, external_id, created_at, birthday "
    "FROM people WHERE id = $1"
)
NAMES = ["read", "recv", "recvfrom", "write", "send", "sendto", "writev", "kevent", "kevent64"]


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


def _counts(lib: ctypes.CDLL) -> list[int]:
    return [lib.syscount_get(i) for i in range(len(NAMES))]


async def _worker(conn: fastpg.Connection, k: int, n: int, tasks: int) -> None:
    for i in range(k, n, tasks):
        await conn.select_one(SQL, i + 1, into=Person)


async def _run(lib: ctypes.CDLL, n: int, driver: str, tasks: int) -> None:
    if driver == "asyncpg":
        import asyncpg

        aconn = await asyncpg.connect(DSN)
        for i in range(200):
            await aconn.fetchrow(SQL, i + 1)
        c0 = _counts(lib)
        for i in range(n):
            Person(*await aconn.fetchrow(SQL, i + 1))
        c1 = _counts(lib)
        await aconn.close()
    else:
        conn = await fastpg.connect(DSN)
        for i in range(200):
            await conn.select_one(SQL, i + 1, into=Person)
        c0 = _counts(lib)
        if tasks:
            await asyncio.gather(*(_worker(conn, k, n, tasks) for k in range(tasks)))
        else:
            for i in range(n):
                await conn.select_one(SQL, i + 1, into=Person)
        c1 = _counts(lib)
        await conn.close()
    counts = zip(NAMES, c0, c1, strict=True)
    print(driver, " ".join(f"{k}={(b - a) / n:.2f}" for k, a, b in counts if b != a))


def main(*, n: int = 2000, driver: str = "fastpg", loop: str = "uvloop", tasks: int = 0) -> None:
    lib = ctypes.CDLL(os.environ["DYLD_INSERT_LIBRARIES"])
    lib.syscount_get.restype = ctypes.c_uint64
    if loop == "uvloop":
        uvloop.run(_run(lib, n, driver, tasks))
    else:
        asyncio.run(_run(lib, n, driver, tasks))
