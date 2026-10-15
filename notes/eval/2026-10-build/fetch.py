#!yeet
"""Large fetches from ``users`` into a msgspec Struct: CPU ns per row; prints one JSON line.

One connection. For each size, a warm-up, then ``budget // rows`` fetches; per fetch, the
loop thread's CPU (``time.thread_time``) and process CPU. Reports the median per-fetch ns/row.
"""

import datetime as dt
import gc
import json
import statistics
import time
import uuid
from typing import Annotated

import msgspec
from yeetr import Opt

import fastpg


class User(msgspec.Struct):
    id: uuid.UUID
    first_name: str
    last_name: str
    email: str
    phone: str
    address: str
    city: str
    country: str
    bio: str
    is_active: bool
    created_at: dt.datetime


async def main(
    *,
    dsn: str = "",
    rows: Annotated[list[int], Opt("-r")] = [1_000, 10_000],  # noqa: B006 - read only
    budget: Annotated[int, Opt("-b")] = 300_000,
) -> None:
    dsn = dsn or "postgresql://perf-test:perf-test@%2Ftmp/perf-test"  # unix socket in /tmp
    query = "SELECT * FROM users LIMIT $1"
    out: dict[str, object] = {"fastpg": fastpg.__file__}
    async with await fastpg.connect(dsn) as conn:
        for n in rows:
            for _ in range(5):
                got = await conn.select_many(query, n, into=User)
                assert len(got) == n  # noqa: S101
            del got
            gc.collect()
            loop_ns: list[float] = []
            cpu_ns: list[float] = []
            for _ in range(max(5, budget // n)):
                p0, t0 = time.process_time_ns(), time.thread_time_ns()
                got = await conn.select_many(query, n, into=User)
                t1, p1 = time.thread_time_ns(), time.process_time_ns()
                del got
                loop_ns.append((t1 - t0) / n)
                cpu_ns.append((p1 - p0) / n)
            out[f"loop_ns_{n}"] = statistics.median(loop_ns)
            out[f"cpu_ns_{n}"] = statistics.median(cpu_ns)
    print("JSON " + json.dumps(out))
