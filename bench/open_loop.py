#!yeet
"""API-like load: point lookups arriving at a fixed rate (open loop), latency at each rate.

The ``concurrent`` scenario in ``run.py`` is a closed-loop saturation test: N tasks fire
their next query the instant the previous one returns. Real APIs see requests *arrive* at
some rate whether or not earlier ones have finished, and what matters is the latency
distribution at that rate, plus how much of the event loop is left for application code.

Method:
* Arrivals are Poisson at the target rate (seeded RNG): realistic bursts, not a metronome.
* Each request runs as its own task on the driver's pool (default 20 connections, a typical
  per-worker pool size). fastpg's ``-pipelined`` variant uses one shared connection.
* Latency is reported in two parts so nothing is hidden (no coordinated omission):
  - **service latency**: dispatch → result, the part the driver controls;
  - **dispatch lag**: scheduled arrival → dispatch. It includes the event loop's ~1ms timer
    granularity (the same for every driver) *and* any time the driver kept the loop busy,
    which delays later arrivals. A loop-hogging driver shows up here.
* "loop busy" is the event-loop thread's CPU time divided by wall time: the share of one core
  the database client and load generator consume, i.e. headroom left for request handling.
* A run is marked overloaded (and stops early) if more than ``--max-inflight`` requests
  are outstanding: the driver can't sustain that rate.

    uv run yeet ./bench/open_loop.py [-r 1000 -r 5000 ...] [-d 'fastpg*' ...]
"""

import asyncio
import fnmatch
import gc
import json
import random
import time
from dataclasses import asdict, dataclass
from operator import attrgetter
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

from drivers import discover
from drivers.base import DEFAULT_DSN, TABLE_ROWS, AsyncSpec
from harness import run_coroutine
from rich.console import Console
from rich.table import Table
from yeetr import Opt

if TYPE_CHECKING:
    from drivers.base import AsyncPool

RESULTS_DIR = Path(__file__).resolve().parent / "results"

DEFAULT_DRIVERS = [
    "asyncpg-struct",
    "asyncpg-pydantic",
    "psycopg-async-struct",
    "psqlpy-struct",
    "fastpg-struct",
    "fastpg-pipelined",
    "fastpg-pydantic",
    "fastpg-pydantic-json",
]


@dataclass(frozen=True, slots=True)
class RateResult:
    driver: str
    target_rps: int
    achieved_rps: float
    requests: int
    p50_us: float
    p90_us: float
    p99_us: float
    p999_us: float
    lag_p99_us: float
    max_us: float
    peak_inflight: int
    loop_busy_pct: float
    overloaded: bool
    errors: int


def _pct(sorted_ns: list[int], q: float) -> float:
    if not sorted_ns:
        return float("nan")
    return sorted_ns[min(len(sorted_ns) - 1, int(q * len(sorted_ns)))] / 1e3


class _Phase:
    """Bookkeeping for one open-loop phase: in-flight requests and their outcomes."""

    def __init__(self, pool: AsyncPool, latencies: list[int] | None) -> None:
        self.pool = pool
        self.latencies = latencies
        self.inflight = self.peak = self.errors = self.sent = 0
        self.idle = asyncio.Event()
        self.idle.set()
        self._tasks: set[asyncio.Task[None]] = set()

    def dispatch(self, now_ns: int, person_id: int) -> None:
        self.inflight += 1
        self.sent += 1
        self.peak = max(self.peak, self.inflight)
        self.idle.clear()
        task = asyncio.get_running_loop().create_task(self._request(now_ns, person_id))
        self._tasks.add(task)  # strong reference until done (the loop only keeps a weak one)
        task.add_done_callback(self._tasks.discard)

    async def _request(self, dispatched_ns: int, person_id: int) -> None:
        try:
            if await self.pool.fetch_one(person_id) is None:
                self.errors += 1
        except Exception:  # noqa: BLE001 - counted, not fatal: we want the rate's outcome
            self.errors += 1
        finally:
            if self.latencies is not None:
                self.latencies.append(time.perf_counter_ns() - dispatched_ns)
            self.inflight -= 1
            if self.inflight == 0:
                self.idle.set()


async def _drive(
    pool: AsyncPool,
    rate: int,
    seconds: float,
    rng: random.Random,
    max_inflight: int,
    latencies: list[int] | None,
    lags: list[int] | None,
) -> tuple[_Phase, bool, float, float]:
    """Run one open-loop phase. Returns (phase, overloaded, wall_s, loop_cpu_s)."""
    phase = _Phase(pool, latencies)
    start_ns = time.perf_counter_ns()
    cpu0 = time.thread_time()
    end_ns = start_ns + int(seconds * 1e9)
    next_ns = start_ns
    overloaded = False
    while next_ns < end_ns and not overloaded:
        now = time.perf_counter_ns()
        if next_ns > now:
            # Timer resolution is ~1ms: sleep until the next arrival, then release every
            # request that is due (the delay is reported as dispatch lag).
            await asyncio.sleep((next_ns - now) / 1e9)
            now = time.perf_counter_ns()
        while next_ns <= now and next_ns < end_ns:
            if lags is not None:
                lags.append(now - next_ns)
            phase.dispatch(now, rng.randint(1, TABLE_ROWS))
            next_ns += int(rng.expovariate(rate) * 1e9)
        overloaded = phase.inflight > max_inflight
        await asyncio.sleep(0)
    await phase.idle.wait()
    wall = (time.perf_counter_ns() - start_ns) / 1e9
    return phase, overloaded, wall, time.thread_time() - cpu0


async def _measure(
    spec: AsyncSpec,
    dsn: str,
    rates: list[int],
    seconds: float,
    pool_size: int,
    seed: int,
    max_inflight: int,
) -> list[RateResult]:
    driver = spec.factory()
    pool = await driver.create_pool(dsn, pool_size)
    results: list[RateResult] = []
    try:
        for rate in rates:
            rng = random.Random(seed)
            await _drive(pool, rate, 1.0, rng, max_inflight, None, None)  # warm-up at this rate
            gc.collect()
            latencies: list[int] = []
            lags: list[int] = []
            phase, overloaded, wall, cpu = await _drive(
                pool,
                rate,
                seconds,
                rng,
                max_inflight,
                latencies,
                lags,
            )
            latencies.sort()
            lags.sort()
            results.append(
                RateResult(
                    driver=spec.name,
                    target_rps=rate,
                    achieved_rps=phase.sent / wall,
                    requests=phase.sent,
                    p50_us=_pct(latencies, 0.50),
                    p90_us=_pct(latencies, 0.90),
                    p99_us=_pct(latencies, 0.99),
                    p999_us=_pct(latencies, 0.999),
                    max_us=latencies[-1] / 1e3 if latencies else float("nan"),
                    lag_p99_us=_pct(lags, 0.99),
                    peak_inflight=phase.peak,
                    loop_busy_pct=100 * cpu / wall,
                    overloaded=overloaded,
                    errors=phase.errors,
                ),
            )
            if overloaded:
                break  # higher rates can only be worse
    finally:
        await pool.close()
    return results


def _fmt(us: float) -> str:
    return f"{us / 1e3:,.2f} ms" if us >= 10_000 else f"{us:,.0f} µs"


def _table(results: list[RateResult], pool_size: int) -> Table:
    table = Table(
        title=f"Open-loop point lookups (Poisson arrivals, pool of {pool_size}); "
        "service latency, lag = scheduled → dispatched",
    )
    table.add_column("driver", no_wrap=True)
    for col in (
        "target/s",
        "achieved/s",
        "p50",
        "p90",
        "p99",
        "p99.9",
        "max",
        "lag p99",
        "in-flight",
        "loop busy",
    ):
        table.add_column(col, justify="right")
    for rate in sorted({r.target_rps for r in results}):
        rows = [r for r in results if r.target_rps == rate]
        for r in sorted(rows, key=attrgetter("overloaded", "p99_us")):
            table.add_row(
                r.driver,
                f"{r.target_rps:,}",
                "OVERLOADED" if r.overloaded else f"{r.achieved_rps:,.0f}",
                _fmt(r.p50_us),
                _fmt(r.p90_us),
                _fmt(r.p99_us),
                _fmt(r.p999_us),
                _fmt(r.max_us),
                _fmt(r.lag_p99_us),
                f"{r.peak_inflight:,}",
                f"{r.loop_busy_pct:.0f}%",
            )
        table.add_section()
    return table


def _markdown(results: list[RateResult], pool_size: int) -> str:
    lines = [
        f"# Open-loop point lookups (Poisson arrivals, pool of {pool_size})",
        "",
        "Service latency (dispatch → result) percentiles; *lag p99* is scheduled → dispatched.",
        "",
        (
            "| target/s | driver | achieved/s | p50 | p90 | p99 | p99.9 | max | lag p99 "
            "| peak in-flight | loop busy |"
        ),
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in sorted(results, key=attrgetter("target_rps", "overloaded", "p99_us")):
        achieved = "OVERLOADED" if r.overloaded else f"{r.achieved_rps:,.0f}"
        lines.append(
            f"| {r.target_rps:,} | {r.driver} | {achieved} | {_fmt(r.p50_us)} | {_fmt(r.p90_us)} "
            f"| {_fmt(r.p99_us)} | {_fmt(r.p999_us)} | {_fmt(r.max_us)} | {_fmt(r.lag_p99_us)} "
            f"| {r.peak_inflight:,} "
            f"| {r.loop_busy_pct:.0f}% |",
        )
    return "\n".join(lines) + "\n"


def main(
    *,
    drivers: Annotated[list[str], Opt("-d")] = DEFAULT_DRIVERS,
    rates: Annotated[list[int], Opt("-r")] = [1_000, 5_000, 10_000, 20_000],  # noqa: B006 - read-only
    seconds: Annotated[float, Opt("-t")] = 5.0,
    pool_size: Annotated[int, Opt("-p")] = 20,
    max_inflight: int = 5_000,
    seed: int = 42,
    dsn: Annotated[str, Opt(envvar="FASTPG_DSN", help="Database URL")] = DEFAULT_DSN,
) -> None:
    """Measure latency percentiles at fixed request rates for each async driver.

    Args:
        drivers: Async driver names or globs (see ``bench/run.py --list-drivers``).
        rates: Target request rates (requests per second), measured in increasing order.
        seconds: Measured duration per rate (after a 1s warm-up at the same rate).
        pool_size: Connections per driver pool (fastpg-pipelined always uses one).
        max_inflight: Outstanding requests at which a rate counts as overloaded.
        seed: RNG seed for arrival times and ids.
        dsn: PostgreSQL DSN.
    """
    registry = discover()
    names = [n for n in registry.specs if any(fnmatch.fnmatch(n, p) for p in drivers)]
    specs = [s for n in names if isinstance(s := registry.specs[n], AsyncSpec)]
    if not specs:
        msg = f"no async drivers match {drivers}"
        raise SystemExit(msg)
    rates = sorted(rates)
    results: list[RateResult] = []
    for spec in specs:
        results += run_coroutine(
            _measure(spec, dsn, rates, seconds, pool_size, seed, max_inflight),
        )
    Console(width=200).print(_table(results, pool_size))
    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    (RESULTS_DIR / f"open-loop-{stamp}.json").write_text(
        json.dumps([asdict(r) for r in results], indent=2),
    )
    (RESULTS_DIR / f"open-loop-{stamp}.md").write_text(_markdown(results, pool_size))
