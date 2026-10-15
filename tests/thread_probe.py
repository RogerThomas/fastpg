"""Thread-count probe for ``test_loop_io_stress.py``, run in a fresh interpreter (Linux only).

``python thread_probe.py DSN asyncio|uvloop`` connects, runs queries (sequential,
pipelined, a streamed result), bulk writes (``copy_in`` big enough to hit COPY
back-pressure, ``insert_many``, ``execute_many``) and a transaction, and closes, then prints
the process's OS thread count before the work and the highest count seen at any checkpoint.
A fresh process matters: a thread anything else in the test session started would hide
one fastpg starts.
"""

import asyncio
import sys
from pathlib import Path

import uvloop

import fastpg


def _threads() -> int:
    return len(list(Path("/proc/self/task").iterdir()))


async def _work(dsn: str, *, seen: list[int]) -> None:
    async with await fastpg.connect(dsn) as conn:
        seen.append(_threads())
        await conn.execute_script("CREATE TEMP TABLE probe (id int, v text)")
        assert await conn.select_one("SELECT $1::int", 1) == (1,)
        await asyncio.gather(*(conn.select_one("SELECT $1::int", i) for i in range(200)))
        assert len(await conn.select_many("SELECT generate_series(1, 20000)")) == 20_000
        seen.append(_threads())
        # ~25 MB of COPY data: well past the 4 MiB high-water mark, so the COPY task waits
        # for the socket (the wait that used to need a tokio timer).
        copied = await conn.copy_in("probe", ((i, "v" * 100) for i in range(200_000)))
        assert copied == 200_000
        seen.append(_threads())
        assert await conn.insert_many("probe", [(1, "v"), (2, "v")]) == 2
        assert await conn.execute_many("INSERT INTO probe VALUES ($1, $2)", [(3, "v")]) == 1
        async with conn.transaction() as tx:
            await tx.execute("INSERT INTO probe VALUES (4, 'v')")
        seen.append(_threads())
    seen.append(_threads())


async def _probe(dsn: str) -> tuple[int, int]:
    before = _threads()
    seen: list[int] = []
    await _work(dsn, seen=seen)
    return before, max(seen)


def main(dsn: str, loop: str) -> None:
    probe = _probe(dsn)
    before, peak = uvloop.run(probe) if loop == "uvloop" else asyncio.run(probe)
    sys.stdout.write(f"{before} {peak}\n")


if __name__ == "__main__":
    main(*sys.argv[1:3])
