#!yeet
"""Client-side cost per row: CPU time spent on the event-loop thread.

Wall-clock numbers through Docker Desktop are dominated by the VM network hop (~300µs per
round trip), which hides how much work each driver does to turn bytes into Python objects.
This measures ``time.thread_time()`` of the event-loop thread around each fetch, i.e. the
CPU the application's own thread spends per query: protocol handling, decoding and object
construction. Work a driver does on other threads (psqlpy's tokio runtime, for one) is
*not* included, so the "total CPU" column (``time.process_time()``) is reported too.

    uv run yeet ./bench/decode_cost.py [-d 'fastpg*' -d asyncpg-struct] [-r 1000 -r 10000]
"""

import fnmatch
import gc
import time
from typing import Annotated

from drivers import discover
from drivers.base import DEFAULT_DSN, AsyncDriver, AsyncSpec
from harness import run_coroutine
from rich.console import Console
from rich.table import Table
from yeetr import Opt

DEFAULT_DRIVERS = [
    "asyncpg-struct",
    "psycopg-async-struct",
    "psqlpy-struct",
    "fastpg",
    "fastpg-struct",
    "fastpg-msgpack",
    "fastpg-json",
    "asyncpg-pydantic",
    "asyncpg-pydantic-construct",
    "psycopg-async-pydantic",
    "fastpg-pydantic",
    "fastpg-pydantic-json",
]


async def _measure(
    driver: AsyncDriver,
    rows: int,
    iterations: int,
) -> tuple[float, float, float]:
    """(wall µs/op, loop-thread CPU µs/op, process CPU µs/op)."""
    for i in range(3):
        await driver.fetch_range(1 + i * rows, rows)
    gc.collect()
    wall = cpu = proc = 0
    for i in range(iterations):
        start = 1 + (i * rows) % (100_000 - rows)
        w0, c0, p0 = time.perf_counter_ns(), time.thread_time_ns(), time.process_time_ns()
        result = await driver.fetch_range(start, rows)
        wall += time.perf_counter_ns() - w0
        cpu += time.thread_time_ns() - c0
        proc += time.process_time_ns() - p0
        assert len(result) == rows  # noqa: S101 - sanity check
        del result
    return wall / iterations / 1e3, cpu / iterations / 1e3, proc / iterations / 1e3


async def _run(specs: list[AsyncSpec], dsn: str, sizes: list[int], budget: int) -> Table:
    table = Table(title="Client CPU per query (event-loop thread)")
    table.add_column("driver", no_wrap=True)
    for col in ("rows", "wall µs/op", "loop CPU µs/op", "loop CPU ns/row", "total CPU µs/op"):
        table.add_column(col, justify="right")
    for spec in specs:
        driver = spec.factory()
        await driver.connect(dsn)
        try:
            for rows in sizes:
                iterations = max(5, budget // rows)
                wall, cpu, proc = await _measure(driver, rows, iterations)
                table.add_row(
                    spec.name,
                    f"{rows:,}",
                    f"{wall:,.0f}",
                    f"{cpu:,.0f}",
                    f"{cpu * 1e3 / rows:,.0f}",
                    f"{proc:,.0f}",
                )
        finally:
            await driver.close()
        table.add_section()
    return table


def main(
    *,
    drivers: Annotated[list[str], Opt("-d")] = DEFAULT_DRIVERS,
    rows: Annotated[list[int], Opt("-r")] = [1, 1_000, 10_000],  # noqa: B006 - immutable use
    budget: Annotated[int, Opt("-b")] = 200_000,
    dsn: Annotated[str, Opt(envvar="FASTPG_DSN", help="Database URL")] = DEFAULT_DSN,
) -> None:
    """Measure loop-thread CPU per fetch for each driver and result size.

    Args:
        drivers: Async driver names or globs (see ``bench/run.py --list-drivers``).
        rows: Result sizes to fetch.
        budget: Rows fetched per (driver, size); iterations = budget / rows (min 5).
        dsn: PostgreSQL DSN.
    """
    registry = discover()
    names = [n for n in registry.specs if any(fnmatch.fnmatch(n, p) for p in drivers)]
    specs = [s for n in names if isinstance(s := registry.specs[n], AsyncSpec)]
    if not specs:
        msg = f"no async drivers match {drivers}"
        raise SystemExit(msg)
    Console().print(run_coroutine(_run(specs, dsn, rows, budget)))
