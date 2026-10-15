#!yeet
"""Rust heap allocations per fetch (alloc-stats build) and GC-tracked objects per row."""

import gc

import uvloop
from ab import DSN, _fetch

import fastpg
from fastpg import _native


async def _run(fetches: int) -> None:
    conn = await fastpg.connect(DSN)
    for target in ("tuple", "struct", "pydantic"):
        for rows in (1, 1000, 10_000):
            await _fetch(conn, 1, rows, target)
            a0 = _native.rust_allocations() or 0
            for i in range(fetches):
                start = 1 + (i * rows) % (100_000 - rows)
                await _fetch(conn, start, rows, target)
            a1 = _native.rust_allocations() or 0
            result = await _fetch(conn, 1, rows, target)
            before = len(gc.get_objects(0)) + len(gc.get_objects(1)) + len(gc.get_objects(2))
            del result
            after = len(gc.get_objects(0)) + len(gc.get_objects(1)) + len(gc.get_objects(2))
            print(
                f"{target:8} {rows:6d} rows: {(a1 - a0) / fetches:9.1f} Rust allocations/fetch "
                f"({(a1 - a0) / fetches / rows:.3f}/row), "
                f"GC-tracked objects/row {(before - after) / rows:.2f}"
            )
    await conn.close()


def main(fetches: int = 20) -> None:
    uvloop.run(_run(fetches))
