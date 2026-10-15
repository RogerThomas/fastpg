#!yeet
"""Mixed workload: do slow queries hurt fast ones? (pool design evidence)

Open-loop Poisson arrivals at a fixed rate. Most requests are point lookups; a fraction
are slow queries (default `SELECT pg_sleep(0.02)`, a stand-in for a report query or a
lock wait). PostgreSQL runs one connection's queries strictly in order, so on a pipelined
connection a slow query delays everything queued behind it (head-of-line blocking).
More connections spread that out. This measures by how much.

Configurations:
* ``fastpg xN``: N connections shared by all requests, each query routed to the
  connection with the fewest queries in flight (pipelined; how a fastpg pool would route).
* ``fastpg-aware xN``: the same, but stall-aware: each connection is scored by how long its
  *oldest* in-flight query has been running (plus a small cost per in-flight query), so a
  connection stuck behind a slow query is avoided even though it has few queries in flight.
* ``fastpg-pool xN cap K``: ``fastpg.create_pool`` with N connections and at most K
  statements in flight per connection, shared mode (``pool.select_many``): idle connection
  first, else the least stalled one under the cap, else wait FIFO.
* ``asyncpg xN``: asyncpg's pool of N, one query per acquired connection.

Fast and slow latencies are reported separately (service latency: dispatch → result).
``-n TASKS`` adds a closed-loop pass: TASKS tasks issue point lookups back to back (no slow
queries) and the table reports ops/s, i.e. how much a cap costs the pipelining win.

    uv run yeet ./bench/mixed.py [-r 5000 -r 10000] [-f 0.01] [-c 1 -c 2 -c 4 -c 8]
    uv run yeet ./bench/mixed.py -o 'fastpg-pool*' -p 8 -k 1 -k 4 -k 16 -n 64
"""

import asyncio
import fnmatch
import json
import random
import time
from dataclasses import asdict, dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import asyncpg
from drivers.base import DEFAULT_DSN, TABLE_ROWS
from harness import run_coroutine
from rich.console import Console
from rich.table import Table
from yeetr import Opt

import fastpg

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

RESULTS_DIR = Path(__file__).resolve().parent / "results"
FAST_SQL = "SELECT id, name, age, email FROM people WHERE id = $1"


@dataclass(frozen=True, slots=True)
class ThroughputResult:
    config: str
    tasks: int
    ops_per_s: float


@dataclass(frozen=True, slots=True)
class MixResult:
    config: str
    rate: int
    slow_fraction: float
    achieved_rps: float
    fast_p50_us: float
    fast_p99_us: float
    fast_p999_us: float
    slow_p50_us: float
    slow_p99_us: float


class _FastpgShared:
    """N fastpg connections; each query goes to the one with the fewest in flight."""

    def __init__(self, conns: list[fastpg.Connection]) -> None:
        self._conns = conns
        self._inflight = [0] * len(conns)

    async def run(self, sql: str, *params: object) -> None:
        i = min(range(len(self._inflight)), key=self._inflight.__getitem__)
        self._inflight[i] += 1
        try:
            await self._conns[i].select_many(sql, *params)
        finally:
            self._inflight[i] -= 1

    async def close(self) -> None:
        for c in self._conns:
            await c.close()


class _FastpgAware(_FastpgShared):
    """Stall-aware routing: avoid connections whose oldest in-flight query is old."""

    PER_QUERY_NS = 50_000  # rough service time of one queued point lookup

    def __init__(self, conns: list[fastpg.Connection]) -> None:
        super().__init__(conns)
        self._started: list[dict[int, int]] = [{} for _ in conns]  # token -> start ns
        self._token = 0

    def _score(self, i: int, now: int) -> int:
        started = self._started[i]
        if not started:
            return 0
        oldest = now - min(started.values())
        return oldest + len(started) * self.PER_QUERY_NS

    async def run(self, sql: str, *params: object) -> None:
        now = time.perf_counter_ns()
        i = min(range(len(self._started)), key=partial(self._score, now=now))
        self._token += 1
        token = self._token
        self._started[i][token] = now
        try:
            await self._conns[i].select_many(sql, *params)
        finally:
            del self._started[i][token]


@dataclass(slots=True)
class _FastpgPool:
    """fastpg's pool in shared mode (routing, per-connection cap, FIFO waiting)."""

    _pool: fastpg.Pool

    async def run(self, sql: str, *params: object) -> None:
        await self._pool.select_many(sql, *params)

    async def close(self) -> None:
        await self._pool.close()


class _AsyncpgPool:
    def __init__(self, pool: asyncpg.Pool[asyncpg.Record]) -> None:
        self._pool = pool

    async def run(self, sql: str, *params: object) -> None:
        await self._pool.fetch(sql, *params)

    async def close(self) -> None:
        await self._pool.close()


def _pct(values: list[int], q: float) -> float:
    if not values:
        return float("nan")
    values.sort()
    return values[min(len(values) - 1, int(q * len(values)))] / 1e3


async def _drive(
    run: Callable[..., Awaitable[None]],
    rate: int,
    seconds: float,
    slow_fraction: float,
    slow_sql: str,
    rng: random.Random,
) -> tuple[list[int], list[int], int, float]:
    fast: list[int] = []
    slow: list[int] = []
    tasks: set[asyncio.Task[None]] = set()

    async def request(is_slow: bool, person_id: int) -> None:  # noqa: FBT001
        t0 = time.perf_counter_ns()
        if is_slow:
            await run(slow_sql)
        else:
            await run(FAST_SQL, person_id)
        (slow if is_slow else fast).append(time.perf_counter_ns() - t0)

    start = time.perf_counter_ns()
    end = start + int(seconds * 1e9)
    next_ns = start
    sent = 0
    while next_ns < end:
        now = time.perf_counter_ns()
        if next_ns > now:
            await asyncio.sleep((next_ns - now) / 1e9)
            now = time.perf_counter_ns()
        while next_ns <= now and next_ns < end:
            task = asyncio.get_running_loop().create_task(
                request(rng.random() < slow_fraction, rng.randint(1, TABLE_ROWS)),
            )
            tasks.add(task)
            task.add_done_callback(tasks.discard)
            sent += 1
            next_ns += int(rng.expovariate(rate) * 1e9)
        await asyncio.sleep(0)
    while tasks:
        await asyncio.gather(*tasks)
    return fast, slow, sent, (time.perf_counter_ns() - start) / 1e9


type _Target = _FastpgShared | _FastpgPool | _AsyncpgPool


@dataclass(frozen=True, slots=True)
class _Config:
    kind: str
    size: int
    cap: int = 0  # fastpg-pool only

    @property
    def label(self) -> str:
        return f"{self.kind} x{self.size}" + (f" cap {self.cap}" if self.cap else "")


async def _open(dsn: str, config: _Config) -> _Target:
    size = config.size
    if config.kind == "fastpg":
        return _FastpgShared([await fastpg.connect(dsn) for _ in range(size)])
    if config.kind == "fastpg-aware":
        return _FastpgAware([await fastpg.connect(dsn) for _ in range(size)])
    if config.kind == "fastpg-pool":
        pool = await fastpg.create_pool(
            dsn,
            min_size=size,
            max_size=size,
            max_inflight_per_conn=config.cap,
        )
        return _FastpgPool(pool)
    return _AsyncpgPool(await asyncpg.create_pool(dsn, min_size=size, max_size=size))


async def _worker(run: Callable[..., Awaitable[None]], deadline_ns: int, seed: int) -> int:
    rng = random.Random(seed)
    ops = 0
    while time.perf_counter_ns() < deadline_ns:
        await run(FAST_SQL, rng.randint(1, TABLE_ROWS))
        ops += 1
    return ops


async def _ops_per_s(run: Callable[..., Awaitable[None]], tasks: int, seconds: float) -> float:
    start = time.perf_counter_ns()
    deadline = start + int(seconds * 1e9)
    counts = await asyncio.gather(*(_worker(run, deadline, seed) for seed in range(tasks)))
    return sum(counts) / ((time.perf_counter_ns() - start) / 1e9)


async def _measure_throughput(
    dsn: str,
    config: _Config,
    task_counts: list[int],
    seconds: float,
) -> list[ThroughputResult]:
    target = await _open(dsn, config)
    out: list[ThroughputResult] = []
    try:
        for tasks in task_counts:
            await _ops_per_s(target.run, tasks, 1.0)  # warm-up
            ops = await _ops_per_s(target.run, tasks, seconds)
            out.append(ThroughputResult(config.label, tasks, ops))
    finally:
        await target.close()
    return out


async def _measure(
    dsn: str,
    config: _Config,
    rates: list[int],
    seconds: float,
    slow_fraction: float,
    slow_sql: str,
) -> list[MixResult]:
    target = await _open(dsn, config)
    out: list[MixResult] = []
    try:
        for rate in rates:
            rng = random.Random(42)
            await _drive(target.run, rate, 1.0, slow_fraction, slow_sql, rng)  # warm-up
            fast, slow, sent, wall = await _drive(
                target.run,
                rate,
                seconds,
                slow_fraction,
                slow_sql,
                rng,
            )
            out.append(
                MixResult(
                    config=config.label,
                    rate=rate,
                    slow_fraction=slow_fraction,
                    achieved_rps=sent / wall,
                    fast_p50_us=_pct(fast, 0.50),
                    fast_p99_us=_pct(fast, 0.99),
                    fast_p999_us=_pct(fast, 0.999),
                    slow_p50_us=_pct(slow, 0.50),
                    slow_p99_us=_pct(slow, 0.99),
                ),
            )
    finally:
        await target.close()
    return out


def _throughput_report(
    dsn: str,
    configs: list[_Config],
    task_counts: list[int],
    seconds: float,
    stamp: str,
) -> None:
    results: list[ThroughputResult] = []
    for config in configs:
        results += run_coroutine(_measure_throughput(dsn, config, task_counts, seconds))
    table = Table(title="Closed loop: point lookups only, back to back")
    table.add_column("config", justify="left")
    for tasks in task_counts:
        table.add_column(f"{tasks} tasks, ops/s", justify="right")
    for config in configs:
        ops = {r.tasks: r.ops_per_s for r in results if r.config == config.label}
        table.add_row(config.label, *(f"{ops[t]:,.0f}" for t in task_counts))
    Console(width=160).print(table)
    (RESULTS_DIR / f"mixed-throughput-{stamp}.json").write_text(
        json.dumps([asdict(r) for r in results], indent=2),
    )


def _fmt(us: float) -> str:
    return f"{us / 1e3:,.1f} ms" if us >= 1_000 else f"{us:,.0f} µs"


def main(
    *,
    rates: Annotated[list[int], Opt("-r")] = [5_000, 10_000],  # noqa: B006 - read-only
    fastpg_sizes: Annotated[list[int], Opt("-c")] = [1, 2, 4, 8],  # noqa: B006 - read-only
    asyncpg_sizes: Annotated[list[int], Opt("-a")] = [8, 16, 32],  # noqa: B006 - read-only
    pool_sizes: Annotated[list[int], Opt("-p")] = [8],  # noqa: B006 - read-only
    caps: Annotated[list[int], Opt("-k")] = [1, 4, 16],  # noqa: B006 - read-only
    only: Annotated[list[str] | None, Opt("-o")] = None,
    closed_loop_tasks: Annotated[list[int] | None, Opt("-n")] = None,
    throughput_only: bool = False,
    slow_fraction: Annotated[float, Opt("-f")] = 0.01,
    slow_sql: str = "SELECT pg_sleep(0.02)",
    seconds: Annotated[float, Opt("-t")] = 5.0,
    dsn: Annotated[str, Opt(envvar="FASTPG_DSN", help="Database URL")] = DEFAULT_DSN,
) -> None:
    """Fast-query latency under a mix of fast and slow queries, by connection count.

    Args:
        rates: Request rates (per second).
        fastpg_sizes: fastpg shared-connection counts to test.
        asyncpg_sizes: asyncpg pool sizes to test.
        pool_sizes: fastpg-pool sizes to test (min_size = max_size).
        caps: fastpg-pool max_inflight_per_conn values to test.
        only: Config label globs to run, e.g. -o 'fastpg-pool*' -o 'asyncpg x16'.
        closed_loop_tasks: Also measure pure point-lookup throughput with this many tasks.
        throughput_only: Skip the mixed workload (use with -n).
        slow_fraction: Fraction of requests that run the slow query.
        slow_sql: The slow query.
        seconds: Measured duration per (config, rate), after a 1s warm-up.
        dsn: PostgreSQL DSN.
    """
    configs = [
        *(_Config("fastpg", n) for n in fastpg_sizes),
        *(_Config("fastpg-aware", n) for n in fastpg_sizes if n > 1),
        *(_Config("fastpg-pool", n, k) for n in pool_sizes for k in caps),
        *(_Config("asyncpg", n) for n in asyncpg_sizes),
    ]
    if only:
        configs = [c for c in configs if any(fnmatch.fnmatch(c.label, o) for o in only)]
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    RESULTS_DIR.mkdir(exist_ok=True)
    if closed_loop_tasks:
        _throughput_report(dsn, configs, closed_loop_tasks, seconds, stamp)
    if throughput_only:
        return
    results: list[MixResult] = []
    for config in configs:
        results += run_coroutine(
            _measure(dsn, config, rates, seconds, slow_fraction, slow_sql),
        )
    title = f"Mixed workload: {slow_fraction:.1%} slow ({slow_sql}), rest point lookups"
    table = Table(title=title)
    for col in ("config", "rate/s", "achieved/s", "fast p50", "fast p99", "fast p99.9",
                "slow p50", "slow p99"):  # fmt: skip
        table.add_column(col, justify="left" if col == "config" else "right")
    for rate in sorted(set(rates)):
        for r in (r for r in results if r.rate == rate):
            table.add_row(
                r.config,
                f"{r.rate:,}",
                f"{r.achieved_rps:,.0f}",
                _fmt(r.fast_p50_us),
                _fmt(r.fast_p99_us),
                _fmt(r.fast_p999_us),
                _fmt(r.slow_p50_us),
                _fmt(r.slow_p99_us),
            )
        table.add_section()
    Console(width=160).print(table)
    (RESULTS_DIR / f"mixed-{stamp}.json").write_text(
        json.dumps([asdict(r) for r in results], indent=2),
    )
