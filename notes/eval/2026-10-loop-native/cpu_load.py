#!yeet
"""Process CPU per wall second for one connection under steady, sporadic and idle load.

fastpg runs network I/O on a background tokio thread that may busy-poll for a while after
activity (``FASTPG_SPIN_US``, default 200 µs; 0 disables). That trades CPU for latency, and
this script measures the CPU side of the trade against other drivers.

Modes, each on one connection per driver:
* ``steady``: closed-loop sequential point lookups (the next one starts when the previous
  returns) for ``--seconds``, after a 1s warm-up.
* ``sporadic``: open-loop Poisson arrivals at ``--rate`` requests/s (seeded RNG), each request
  its own task, for ``--seconds`` after a 1s warm-up at the same rate. Requests that overlap
  queue on a lock: most drivers reject concurrent use of one connection (asyncpg raises
  "another operation is in progress"), so every driver runs one request at a time and the
  queueing shows up in latency (dispatch -> result).
* ``idle``: 200 warm-up lookups, then nothing for ``--seconds``. Shows background CPU after
  activity (e.g. spinning that never stops) plus the idle baseline.

CPU is ``time.process_time()`` over the measured window divided by wall time, so it covers
every thread in the process (fastpg's I/O thread included), reported as % of one core. The
load generator's own CPU (uvloop, task creation, timers) is in it too, the same for every
driver. "CPU µs/req" divides the window's CPU by the requests completed in it.

fastpg reads ``FASTPG_SPIN_US`` once per process, so compare spin settings with separate runs:

    FASTPG_SPIN_US=0 uv run yeet ./bench/cpu_load.py [-d 'fastpg*' ...] [-m steady ...]
    uv run yeet ./bench/cpu_load.py [-t 5] [--rate 100]
"""

import asyncio
import fnmatch
import gc
import json
import os
import random
import time
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Annotated

from drivers import discover
from drivers.base import DEFAULT_DSN, TABLE_ROWS, AsyncDriver, AsyncSpec
from harness import run_coroutine
from rich.console import Console
from rich.table import Table
from yeetr import Opt


class Mode(StrEnum):
    STEADY = "steady"
    SPORADIC = "sporadic"
    IDLE = "idle"


class Settings:
    """Script-wide constants."""

    RESULTS_DIR = Path(__file__).resolve().parent / "results"
    WARMUP_S = 1.0
    IDLE_WARMUP_LOOKUPS = 200
    SPIN_ENV = "FASTPG_SPIN_US"
    SPIN_DEFAULT_US = 200


@dataclass(slots=True)
class CPUResult:
    driver: str
    mode: str
    spin_us: str
    wall_s: float
    cpu_s: float
    cpu_pct: float
    requests: int
    achieved_rps: float
    cpu_us_per_req: float | None
    p50_us: float | None
    p99_us: float | None
    errors: int


@dataclass(slots=True)
class Window:
    """What one measured window observed."""

    wall_s: float
    cpu_s: float
    latencies_ns: list[int] = field(default_factory=list[int])
    errors: int = 0


class _Sporadic:
    """In-flight open-loop requests on one connection, and their outcomes."""

    def __init__(self, driver: AsyncDriver, latencies: list[int]) -> None:
        self.driver = driver
        self.latencies = latencies
        self.inflight = self.errors = 0
        self.idle = asyncio.Event()
        self.idle.set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._connection = asyncio.Lock()  # one request at a time on the shared connection

    async def _request(self, person_id: int) -> None:
        dispatched_ns = time.perf_counter_ns()
        try:
            async with self._connection:
                row = await self.driver.fetch_one(person_id)
        except Exception:  # noqa: BLE001 - counted, not fatal: we want the window's outcome
            self.errors += 1
        else:
            if row is None:
                self.errors += 1
        finally:
            self.latencies.append(time.perf_counter_ns() - dispatched_ns)
            self.inflight -= 1
            if self.inflight == 0:
                self.idle.set()

    def dispatch(self, person_id: int) -> None:
        self.inflight += 1
        self.idle.clear()
        task = asyncio.get_running_loop().create_task(self._request(person_id))
        self._tasks.add(task)  # strong reference until done (the loop only keeps a weak one)
        task.add_done_callback(self._tasks.discard)


def _spin_label() -> str:
    value = os.environ.get(Settings.SPIN_ENV)
    return f"{Settings.SPIN_DEFAULT_US} (default)" if value is None else value


def _pct(sorted_ns: list[int], q: float) -> float:
    return sorted_ns[min(len(sorted_ns) - 1, int(q * len(sorted_ns)))] / 1e3


async def _steady(driver: AsyncDriver, seconds: float, rng: random.Random) -> Window:
    window = Window(0.0, 0.0)
    start_ns = now = time.perf_counter_ns()
    cpu0 = time.process_time()
    end_ns = start_ns + int(seconds * 1e9)
    while now < end_ns:
        t0 = time.perf_counter_ns()
        row = await driver.fetch_one(rng.randint(1, TABLE_ROWS))
        now = time.perf_counter_ns()
        window.latencies_ns.append(now - t0)
        if row is None:
            window.errors += 1
    window.cpu_s = time.process_time() - cpu0
    window.wall_s = (now - start_ns) / 1e9
    return window


async def _sporadic(
    driver: AsyncDriver,
    seconds: float,
    rate: float,
    rng: random.Random,
) -> Window:
    latencies: list[int] = []
    phase = _Sporadic(driver, latencies)
    start_ns = time.perf_counter_ns()
    cpu0 = time.process_time()
    end_ns = start_ns + int(seconds * 1e9)
    next_ns = start_ns
    while next_ns < end_ns:
        now = time.perf_counter_ns()
        if next_ns > now:
            await asyncio.sleep((next_ns - now) / 1e9)
            now = time.perf_counter_ns()
        while next_ns <= now and next_ns < end_ns:
            phase.dispatch(rng.randint(1, TABLE_ROWS))
            next_ns += int(rng.expovariate(rate) * 1e9)
    await phase.idle.wait()
    cpu = time.process_time() - cpu0
    wall = (time.perf_counter_ns() - start_ns) / 1e9
    return Window(wall, cpu, latencies, phase.errors)


async def _idle(driver: AsyncDriver, seconds: float, rng: random.Random) -> Window:
    for _ in range(Settings.IDLE_WARMUP_LOOKUPS):
        await driver.fetch_one(rng.randint(1, TABLE_ROWS))
    gc.collect()
    start_ns = time.perf_counter_ns()
    cpu0 = time.process_time()
    await asyncio.sleep(seconds)
    cpu = time.process_time() - cpu0
    return Window((time.perf_counter_ns() - start_ns) / 1e9, cpu)


async def _window(
    driver: AsyncDriver,
    mode: Mode,
    seconds: float,
    rate: float,
    rng: random.Random,
) -> Window:
    """Warm up, then measure one window of ``mode``."""
    match mode:
        case Mode.STEADY:
            await _steady(driver, Settings.WARMUP_S, rng)
            gc.collect()
            return await _steady(driver, seconds, rng)
        case Mode.SPORADIC:
            await _sporadic(driver, Settings.WARMUP_S, rate, rng)
            gc.collect()
            return await _sporadic(driver, seconds, rate, rng)
        case Mode.IDLE:
            return await _idle(driver, seconds, rng)


def _result(spec: AsyncSpec, mode: Mode, spin: str, window: Window) -> CPUResult:
    lat = sorted(window.latencies_ns)
    requests = len(lat)
    return CPUResult(
        driver=spec.name,
        mode=mode.value,
        spin_us=spin,
        wall_s=round(window.wall_s, 3),
        cpu_s=round(window.cpu_s, 4),
        cpu_pct=100 * window.cpu_s / window.wall_s,
        requests=requests,
        achieved_rps=requests / window.wall_s,
        cpu_us_per_req=window.cpu_s * 1e6 / requests if requests else None,
        p50_us=_pct(lat, 0.50) if lat else None,
        p99_us=_pct(lat, 0.99) if lat else None,
        errors=window.errors,
    )


async def _measure(
    spec: AsyncSpec,
    dsn: str,
    modes: list[Mode],
    seconds: float,
    rate: float,
    seed: int,
) -> list[CPUResult]:
    spin = _spin_label()
    results: list[CPUResult] = []
    for mode in modes:
        driver = spec.factory()
        await driver.connect(dsn)
        try:
            window = await _window(driver, mode, seconds, rate, random.Random(seed))
        finally:
            await driver.close()
        results.append(_result(spec, mode, spin, window))
    return results


def _fmt_us(us: float | None) -> str:
    if us is None:
        return "-"
    return f"{us / 1e3:,.2f} ms" if us >= 10_000 else f"{us:,.0f} µs"


def _cells(r: CPUResult) -> list[str]:
    active = r.mode != Mode.IDLE
    return [
        r.driver,
        r.mode,
        f"{r.cpu_pct:.1f}%",
        f"{r.achieved_rps:,.0f}" if active else "-",
        _fmt_us(r.cpu_us_per_req) if active else "-",
        _fmt_us(r.p50_us),
        _fmt_us(r.p99_us),
        f"{r.errors:,}",
    ]


def _title(seconds: float, rate: float) -> str:
    return (
        f"Process CPU per wall second, one connection ({seconds:g}s windows, "
        f"sporadic at {rate:g} req/s); FASTPG_SPIN_US={_spin_label()}"
    )


def _table(results: list[CPUResult], seconds: float, rate: float) -> Table:
    table = Table(title=_title(seconds, rate))
    table.add_column("driver", no_wrap=True)
    table.add_column("mode", no_wrap=True)
    for col in ("CPU (% of one core)", "req/s", "CPU µs/req", "p50", "p99", "errors"):
        table.add_column(col, justify="right")
    for mode in Mode:
        rows = [r for r in results if r.mode == mode]
        for r in rows:
            table.add_row(*_cells(r))
        if rows:
            table.add_section()
    return table


def _markdown(results: list[CPUResult], seconds: float, rate: float) -> str:
    lines = [
        f"# {_title(seconds, rate)}",
        "",
        (
            "CPU is `time.process_time()` (all threads) over the window divided by wall time. "
            "Latency is dispatch → result."
        ),
        "",
        "| driver | mode | CPU (% of one core) | req/s | CPU µs/req | p50 | p99 | errors |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for mode in Mode:
        lines.extend("| " + " | ".join(_cells(r)) + " |" for r in results if r.mode == mode)
    return "\n".join(lines) + "\n"


def main(
    *,
    drivers: Annotated[list[str], Opt("-d")] = ["fastpg-struct", "asyncpg-struct"],  # noqa: B006 - read-only
    modes: Annotated[list[Mode], Opt("-m")] = [Mode.STEADY, Mode.SPORADIC, Mode.IDLE],  # noqa: B006 - read-only
    seconds: Annotated[float, Opt("-t")] = 5.0,
    rate: float = 100.0,
    seed: int = 42,
    dsn: Annotated[str, Opt(envvar="FASTPG_DSN", help="Database URL")] = DEFAULT_DSN,
) -> None:
    """Measure process CPU per second for each async driver under each load mode.

    Args:
        drivers: Async driver names or globs (see ``bench/run.py --list-drivers``).
        modes: Load modes to run: steady, sporadic, idle.
        seconds: Measured duration per (driver, mode), after the warm-up.
        rate: Mean arrival rate (requests/s) of the sporadic mode.
        seed: RNG seed for arrival times and ids.
        dsn: PostgreSQL DSN.
    """
    registry = discover()
    names = [n for n in registry.specs if any(fnmatch.fnmatch(n, p) for p in drivers)]
    specs = [s for n in names if isinstance(s := registry.specs[n], AsyncSpec)]
    if not specs:
        msg = f"no async drivers match {drivers}"
        raise SystemExit(msg)
    results: list[CPUResult] = []
    for spec in specs:
        results += run_coroutine(_measure(spec, dsn, modes, seconds, rate, seed))
    Console(width=160).print(_table(results, seconds, rate))
    Settings.RESULTS_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    (Settings.RESULTS_DIR / f"cpu-{stamp}.json").write_text(
        json.dumps([asdict(r) for r in results], indent=2),
    )
    (Settings.RESULTS_DIR / f"cpu-{stamp}.md").write_text(_markdown(results, seconds, rate))
