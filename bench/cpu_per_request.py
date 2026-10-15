#!yeet
"""Total process CPU per request (and latency) for point lookups, per driver.

Under a CPU quota (a 500m pod, a Lambda) throughput is roughly quota / CPU-per-request, so
this reports ``time.process_time()`` over the measured requests (every thread, user and
system time) next to the event-loop thread's own ``time.thread_time()``. Three modes:

* sequential (default): one request at a time on one connection, p50/p99 latency too;
* ``-t 16``: 16 tasks over the driver's pool of one connection, throughput (fastpg's pool
  pipelines up to 8 statements on it, ``fastpg-pipelined`` all 16; the others queue);
* ``-i 0.01``: sporadic traffic, a pause between requests (shows wake-up costs).

Examples::

    uv run yeet ./bench/cpu_per_request.py                       # fastpg vs asyncpg
    uv run yeet ./bench/cpu_per_request.py -t 16 -n 20000
    uv run yeet ./bench/cpu_per_request.py -l asyncio            # stdlib loop, not uvloop
"""

import asyncio
import fnmatch
import gc
import time
from typing import Annotated

from drivers import discover
from drivers.base import DEFAULT_DSN, AsyncDriver, AsyncPool, AsyncSpec
from rich.console import Console
from rich.table import Table
from yeetr import Opt

from fastpg._native import rust_allocations

DEFAULT_DRIVERS = ["fastpg-struct", "asyncpg-struct"]
WARMUP = 200
TABLE_ROWS = 100_000


async def _sequential(driver: AsyncDriver, requests: int, idle: float) -> list[str]:
    for i in range(WARMUP):
        await driver.fetch_one(i + 1)
    gc.collect()
    latencies: list[float] = []
    cpu0, loop0, wall0 = time.process_time(), time.thread_time(), time.perf_counter()
    for i in range(requests):
        start = time.perf_counter_ns()
        await driver.fetch_one(i % TABLE_ROWS + 1)
        latencies.append((time.perf_counter_ns() - start) / 1e3)
        if idle:
            await asyncio.sleep(idle)
    wall = time.perf_counter() - wall0
    cpu = time.process_time() - cpu0
    loop_cpu = time.thread_time() - loop0
    latencies.sort()
    return [
        f"{latencies[requests // 2]:,.0f}",
        f"{latencies[int(requests * 0.99)]:,.0f}",
        f"{cpu * 1e6 / requests:,.1f}",
        f"{loop_cpu * 1e6 / requests:,.1f}",
        f"{100 * cpu / wall:,.1f}",
        f"{requests / wall:,.0f}",
    ]


async def _concurrent(pool: AsyncPool, requests: int, tasks: int) -> list[str]:
    async def worker(k: int) -> None:
        for i in range(k, requests, tasks):
            await pool.fetch_one(i % TABLE_ROWS + 1)

    await asyncio.gather(*(worker(k) for k in range(tasks)))  # warm-up
    gc.collect()
    cpu0, loop0, wall0 = time.process_time(), time.thread_time(), time.perf_counter()
    await asyncio.gather(*(worker(k) for k in range(tasks)))
    wall = time.perf_counter() - wall0
    cpu = time.process_time() - cpu0
    loop_cpu = time.thread_time() - loop0
    return [
        "-",
        "-",
        f"{cpu * 1e6 / requests:,.1f}",
        f"{loop_cpu * 1e6 / requests:,.1f}",
        f"{100 * cpu / wall:,.1f}",
        f"{requests / wall:,.0f}",
    ]


async def _run(
    specs: list[AsyncSpec],
    dsn: str,
    requests: int,
    tasks: int,
    idle: float,
) -> Table:
    mode = f"{tasks} tasks, one connection" if tasks else f"sequential, pause {idle}s"
    table = Table(title=f"Process CPU per request ({mode}; {requests:,} requests)")
    table.add_column("driver", no_wrap=True)
    for col in ("p50 µs", "p99 µs", "process CPU µs/req", "loop CPU µs/req", "CPU %", "req/s"):
        table.add_column(col, justify="right")
    for spec in specs:
        driver = spec.factory()
        if tasks:
            pool = await driver.create_pool(dsn, 1)
            try:
                cells = await _concurrent(pool, requests, tasks)
            finally:
                await pool.close()
        else:
            await driver.connect(dsn)
            try:
                cells = await _sequential(driver, requests, idle)
            finally:
                await driver.close()
        table.add_row(spec.name, *cells)
    return table


def main(
    *,
    drivers: Annotated[list[str], Opt("-d")] = DEFAULT_DRIVERS,
    requests: Annotated[int, Opt("-n")] = 5000,
    tasks: Annotated[int, Opt("-t")] = 0,
    idle: Annotated[float, Opt("-i")] = 0.0,
    loop: Annotated[str, Opt("-l")] = "uvloop",
    dsn: Annotated[str, Opt(envvar="FASTPG_DSN", help="Database URL")] = DEFAULT_DSN,
) -> None:
    """Measure process CPU per point lookup, per driver.

    Args:
        drivers: Async driver names or globs (see ``bench/run.py --list-drivers``).
        requests: Measured requests per driver (after a warm-up).
        tasks: 0 for the sequential scenario, else concurrent tasks on one connection.
        idle: Seconds to sleep between sequential requests (sporadic traffic).
        loop: ``uvloop`` or ``asyncio`` (the stdlib event loop).
        dsn: Database URL.
    """
    registry = discover()
    specs = [
        spec
        for name, spec in registry.specs.items()
        if isinstance(spec, AsyncSpec) and any(fnmatch.fnmatch(name, p) for p in drivers)
    ]
    if not specs:
        msg = f"no async driver matches {drivers!r}"
        raise SystemExit(msg)
    coro = _run(specs, dsn, requests, tasks, idle)
    if loop == "uvloop":
        import uvloop  # optional dependency

        table = uvloop.run(coro)
    else:
        table = asyncio.run(coro)
    console = Console()
    console.print(table)
    alloc = rust_allocations()
    note = f"loop={loop}; fastpg alloc-stats {'on' if alloc is not None else 'off'}"
    console.print(note, style="dim")
