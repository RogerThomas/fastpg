#!yeet
"""Socket reads per fetch (instrumented build): count, bytes, EAGAIN, short reads."""

import uvloop
from ab import DSN, SQL, Person

import fastpg
from fastpg import _native


async def _run(rows: int, fetches: int) -> None:
    conn = await fastpg.connect(DSN)
    await conn.select_many(SQL, 1, rows, into=Person)
    r0, b0, e0, s0, a0 = _native.io_stats()
    for i in range(fetches):
        start = 1 + (i * rows) % (100_000 - rows)
        await conn.select_many(SQL, start, start + rows - 1, into=Person)
    r1, b1, e1, s1, a1 = _native.io_stats()
    reads, nbytes, eagain, short = r1 - r0, b1 - b0, e1 - e0, s1 - s0
    print(
        f"{rows} rows: {reads / fetches:.1f} reads/fetch, {nbytes / fetches / 1024:.0f} KiB/fetch, "
        f"{nbytes / max(reads - eagain, 1):.0f} B per successful read, "
        f"asked {(a1 - a0) / reads:.0f} B per read, "
        f"{eagain / fetches:.1f} EAGAIN/fetch, {short / fetches:.1f} short reads/fetch"
    )
    await conn.close()


def main(rows: int = 10_000, fetches: int = 50) -> None:
    uvloop.run(_run(rows, fetches))
