"""Scenario runners: warmup, timed loops, the concurrent scenarios and tracemalloc passes.

Timing uses ``time.perf_counter_ns``. Per-op latency covers the adapter call only: the
returned rows are released after the clock stops, so freeing them isn't counted in latency
(it is in throughput, which is ops / wall-clock time of the whole measured loop).
tracemalloc is only ever on during the separate allocation pass, never while timing.
"""

import asyncio
import gc
import importlib.util
import logging
import random
import statistics
import tracemalloc
from dataclasses import dataclass
from enum import StrEnum
from operator import attrgetter
from time import perf_counter_ns
from typing import TYPE_CHECKING, Any, Literal

import msgspec
from drivers.base import (
    TABLE_ROWS,
    Kind,
    Person,
    PersonModel,
    RowShape,
    as_person,
    expected_person,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine, Sequence

    from drivers.base import AsyncDriver, AsyncPool, AsyncSpec, DriverSpec, SyncDriver, SyncSpec

logger = logging.getLogger("bench")

MIN_WARMUP_OPS = 10  # comfortably past psycopg's prepare_threshold (5)
_STREAM_LEN = 1 << 16  # pre-generated ids per scenario, cycled with a mask
_MASK = _STREAM_LEN - 1
_VERIFY_IDS = (1, 7, 11, 14, 77, 50_000, TABLE_ROWS)
# _HOLD_PREVIOUS: measured loops keep the previous op's result alive until the next op has
# been measured. Some drivers keep a reference to their last result (pg8000's
# ``conn._context``, asyncpg's protocol) and release it during the *next* call; without this,
# freeing old rows would land inside the next op's latency and offset its net allocations
# for those drivers only.
LOOP: Literal["uvloop", "asyncio"] = "uvloop" if importlib.util.find_spec("uvloop") else "asyncio"


class Scenario(StrEnum):
    POINT = "point"
    RANGE_1K = "range-1k"
    RANGE_10K = "range-10k"
    CONCURRENT = "concurrent"
    CONCURRENT_PINNED = "concurrent-pinned"
    ALLOC = "alloc"


SEQUENTIAL = (Scenario.POINT, Scenario.RANGE_1K, Scenario.RANGE_10K)
ROWS_PER_OP: dict[Scenario, int] = {
    Scenario.POINT: 1,
    Scenario.RANGE_1K: 1_000,
    Scenario.RANGE_10K: 10_000,
    Scenario.CONCURRENT: 1,
    Scenario.CONCURRENT_PINNED: 1,
}
# The alloc scenario reports two result rows: (result scenario name, rows per op).
ALLOC_CASES = (("alloc-point", 1), ("alloc-range-1k", 1_000))


class Result(msgspec.Struct, kw_only=True, omit_defaults=True):
    """One (driver, scenario) measurement. Latencies in µs, allocations in bytes per op."""

    driver: str
    kind: Kind
    rows: RowShape
    scenario: str
    rows_per_op: int = 0
    ops: int = 0
    seconds: float = 0.0
    ops_per_s: float | None = None
    rows_per_s: float | None = None
    p50_us: float | None = None
    p90_us: float | None = None
    p99_us: float | None = None
    peak_bytes: float | None = None
    net_bytes: float | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Plan:
    dsn: str
    scenarios: tuple[Scenario, ...]
    duration_s: float
    iterations: int | None
    warmup_s: float
    concurrency: int
    pool_size: int
    alloc_iterations: int
    seed: int

    def done(self, ops: int, now_ns: int, deadline_ns: int) -> bool:
        """Measured-loop exit condition: the fixed op count if given, else the deadline."""
        if self.iterations is not None:
            return ops >= self.iterations
        return now_ns >= deadline_ns

    def deadline(self, start_ns: int) -> int:
        return start_ns + int(self.duration_s * 1e9)

    def warmup_deadline(self) -> int:
        return perf_counter_ns() + int(self.warmup_s * 1e9)


@dataclass(frozen=True, slots=True)
class Timing:
    latencies_ns: list[int]
    elapsed_ns: int


class VerificationError(AssertionError):
    """An adapter returned wrong rows, so its numbers would be meaningless."""


def run_coroutine[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run ``coro`` on a fresh uvloop loop when available, else on asyncio's default."""
    if LOOP == "uvloop":
        import uvloop  # optional dependency, see LOOP

        return uvloop.run(coro)
    return asyncio.run(coro)


def id_stream(seed: int, rows_per_op: int) -> list[int]:
    """Deterministic start ids; every driver sees the same sequence for a given scenario."""
    rng = random.Random(f"{seed}:{rows_per_op}")
    hi = TABLE_ROWS - rows_per_op + 1
    return [rng.randint(1, hi) for _ in range(_STREAM_LEN)]


def _timing_result(
    spec: DriverSpec,
    kind: Kind,
    scenario: str,
    rows_per_op: int,
    timing: Timing,
) -> Result:
    lat = timing.latencies_ns
    ops = len(lat)
    seconds = timing.elapsed_ns / 1e9
    ops_per_s = ops / seconds
    cuts = statistics.quantiles(lat, n=100, method="inclusive") if ops > 1 else lat * 99
    return Result(
        driver=spec.name,
        kind=kind,
        rows=spec.rows,
        scenario=scenario,
        rows_per_op=rows_per_op,
        ops=ops,
        seconds=round(seconds, 3),
        ops_per_s=round(ops_per_s, 1),
        rows_per_s=round(ops_per_s * rows_per_op, 1),
        p50_us=round(cuts[49] / 1e3, 1),
        p90_us=round(cuts[89] / 1e3, 1),
        p99_us=round(cuts[98] / 1e3, 1),
    )


def _alloc_result(
    spec: DriverSpec,
    kind: Kind,
    case: tuple[str, int],
    plan: Plan,
    totals: tuple[int, int],
) -> Result:
    scenario, rows_per_op = case
    peak_total, net_total = totals
    return Result(
        driver=spec.name,
        kind=kind,
        rows=spec.rows,
        scenario=scenario,
        rows_per_op=rows_per_op,
        ops=plan.alloc_iterations,
        peak_bytes=round(peak_total / plan.alloc_iterations, 1),
        net_bytes=round(net_total / plan.alloc_iterations, 1),
    )


# ---------------------------------------------------------------------------- verification


def _as_struct(spec: DriverSpec, row: object) -> object:
    """Pydantic rows are compared through their msgspec equivalent."""
    if spec.rows == "pydantic":
        if not isinstance(row, PersonModel):
            msg = f"{spec.name}: expected PersonModel rows, got {type(row).__name__}"
            raise VerificationError(msg)
        return as_person(row)
    return row


def _check_one(spec: DriverSpec, person_id: int, row: object) -> None:
    if row is None:
        msg = f"{spec.name}: fetch_one({person_id}) returned None"
        raise VerificationError(msg)
    row = _as_struct(spec, row)
    if spec.rows != "native" and row != (want := expected_person(person_id)):
        msg = f"{spec.name}: fetch_one({person_id}) returned {row!r}, expected {want!r}"
        raise VerificationError(msg)


def _check_range(spec: DriverSpec, start: int, n: int, rows: Sequence[object]) -> None:
    if len(rows) != n:
        msg = f"{spec.name}: fetch_range({start}, {n}) returned {len(rows)} rows"
        raise VerificationError(msg)
    if spec.rows != "native":
        converted = [_as_struct(spec, r) for r in rows]
        people = sorted((r for r in converted if isinstance(r, Person)), key=attrgetter("id"))
        if people != [expected_person(i) for i in range(start, start + n)]:
            msg = f"{spec.name}: fetch_range({start}, {n}) returned wrong or non-Person rows"
            raise VerificationError(msg)


def verify_sync(spec: SyncSpec, driver: SyncDriver) -> None:
    for person_id in _VERIFY_IDS:
        _check_one(spec, person_id, driver.fetch_one(person_id))
    _check_range(spec, 1, 1_000, driver.fetch_range(1, 1_000))
    _check_range(spec, TABLE_ROWS - 99, 100, driver.fetch_range(TABLE_ROWS - 99, 100))


async def verify_async(spec: AsyncSpec, driver: AsyncDriver) -> None:
    for person_id in _VERIFY_IDS:
        _check_one(spec, person_id, await driver.fetch_one(person_id))
    _check_range(spec, 1, 1_000, await driver.fetch_range(1, 1_000))
    _check_range(spec, TABLE_ROWS - 99, 100, await driver.fetch_range(TABLE_ROWS - 99, 100))


# -------------------------------------------------------------------------------- sync


def _sync_op(driver: SyncDriver, rows_per_op: int) -> Callable[[int], object]:
    if rows_per_op == 1:
        return driver.fetch_one

    def fetch(start: int) -> object:
        return driver.fetch_range(start, rows_per_op)

    return fetch


def time_sync(op: Callable[[int], object], ids: list[int], plan: Plan) -> Timing:
    i = 0
    warm_end = plan.warmup_deadline()
    while i < MIN_WARMUP_OPS or perf_counter_ns() < warm_end:
        op(ids[i & _MASK])
        i += 1
    gc.collect()
    lat: list[int] = []
    prev: object = None  # see _HOLD_PREVIOUS
    start = now = perf_counter_ns()
    deadline = plan.deadline(start)
    while not plan.done(len(lat), now, deadline):
        arg = ids[i & _MASK]
        t0 = perf_counter_ns()
        rows = op(arg)
        now = perf_counter_ns()
        lat.append(now - t0)
        prev = rows
        i += 1
    del prev
    return Timing(lat, now - start)


def alloc_sync(op: Callable[[int], object], ids: list[int], plan: Plan) -> tuple[int, int]:
    """Total (peak, net) traced bytes over ``plan.alloc_iterations`` ops."""
    prev: object = None  # see _HOLD_PREVIOUS
    for i in range(MIN_WARMUP_OPS):  # populate statement/type caches before tracing
        prev = op(ids[i & _MASK])
    peak_total = net_total = 0
    gc.collect()
    gc.disable()
    tracemalloc.start()
    try:
        for i in range(plan.alloc_iterations):
            arg = ids[i & _MASK]
            tracemalloc.reset_peak()
            before, _ = tracemalloc.get_traced_memory()
            rows = op(arg)
            current, peak = tracemalloc.get_traced_memory()
            prev = rows
            peak_total += peak - before
            net_total += current - before
    finally:
        del prev
        tracemalloc.stop()
        gc.enable()
    return peak_total, net_total


def run_sync_driver(spec: SyncSpec, plan: Plan, out: list[Result]) -> None:
    """Run every selected scenario for one sync adapter, appending to ``out`` as it goes."""
    driver = spec.factory()
    driver.connect(plan.dsn)
    try:
        verify_sync(spec, driver)
        for scenario in SEQUENTIAL:
            if scenario not in plan.scenarios:
                continue
            n = ROWS_PER_OP[scenario]
            logger.info("%-22s %s", spec.name, scenario)
            timing = time_sync(_sync_op(driver, n), id_stream(plan.seed, n), plan)
            out.append(_timing_result(spec, "sync", scenario, n, timing))
        if Scenario.ALLOC in plan.scenarios:
            logger.info("%-22s %s", spec.name, Scenario.ALLOC)
            for case in ALLOC_CASES:
                n = case[1]
                totals = alloc_sync(_sync_op(driver, n), id_stream(plan.seed, n), plan)
                out.append(_alloc_result(spec, "sync", case, plan, totals))
    finally:
        driver.close()


# ------------------------------------------------------------------------------- async


def _async_op(driver: AsyncDriver, rows_per_op: int) -> Callable[[int], Awaitable[object]]:
    if rows_per_op == 1:
        return driver.fetch_one

    def fetch(start: int) -> Awaitable[object]:
        return driver.fetch_range(start, rows_per_op)

    return fetch


async def time_async(
    op: Callable[[int], Awaitable[object]],
    ids: list[int],
    plan: Plan,
) -> Timing:
    i = 0
    warm_end = plan.warmup_deadline()
    while i < MIN_WARMUP_OPS or perf_counter_ns() < warm_end:
        await op(ids[i & _MASK])
        i += 1
    gc.collect()
    lat: list[int] = []
    prev: object = None  # see _HOLD_PREVIOUS
    start = now = perf_counter_ns()
    deadline = plan.deadline(start)
    while not plan.done(len(lat), now, deadline):
        arg = ids[i & _MASK]
        t0 = perf_counter_ns()
        rows = await op(arg)
        now = perf_counter_ns()
        lat.append(now - t0)
        prev = rows
        i += 1
    del prev
    return Timing(lat, now - start)


async def time_concurrent(
    ops: Sequence[Callable[[int], Awaitable[object]]],
    ids: list[int],
    plan: Plan,
) -> Timing:
    """One task per entry of ``ops``, each doing point lookups back to back through its op."""
    tasks = len(ops)
    stride = _STREAM_LEN // tasks
    per_task = None if plan.iterations is None else max(1, plan.iterations // tasks)

    async def warm(k: int, end_ns: int) -> None:
        i = k * stride
        n = 0
        while n < MIN_WARMUP_OPS or perf_counter_ns() < end_ns:
            await ops[k](ids[i & _MASK])
            i += 1
            n += 1

    async def measure(k: int, deadline_ns: int, lat: list[int]) -> None:
        i = k * stride + stride // 2  # don't replay this task's warmup ids
        n = 0
        prev: object = None  # see _HOLD_PREVIOUS
        now = perf_counter_ns()
        while (n < per_task) if per_task is not None else (now < deadline_ns):
            arg = ids[i & _MASK]
            t0 = perf_counter_ns()
            row = await ops[k](arg)
            now = perf_counter_ns()
            lat.append(now - t0)
            prev = row
            i += 1
            n += 1
        del prev

    warm_end = plan.warmup_deadline()
    async with asyncio.TaskGroup() as tg:
        for k in range(tasks):
            tg.create_task(warm(k, warm_end))
    gc.collect()
    lat: list[int] = []
    start = perf_counter_ns()
    deadline = plan.deadline(start)
    async with asyncio.TaskGroup() as tg:
        for k in range(tasks):
            tg.create_task(measure(k, deadline, lat))
    return Timing(lat, perf_counter_ns() - start)


async def alloc_async(
    op: Callable[[int], Awaitable[object]],
    ids: list[int],
    plan: Plan,
) -> tuple[int, int]:
    """Total (peak, net) traced bytes over ``plan.alloc_iterations`` ops."""
    prev: object = None  # see _HOLD_PREVIOUS
    for i in range(MIN_WARMUP_OPS):  # populate statement/type caches before tracing
        prev = await op(ids[i & _MASK])
    peak_total = net_total = 0
    gc.collect()
    gc.disable()
    tracemalloc.start()
    try:
        for i in range(plan.alloc_iterations):
            arg = ids[i & _MASK]
            tracemalloc.reset_peak()
            before, _ = tracemalloc.get_traced_memory()
            rows = await op(arg)
            current, peak = tracemalloc.get_traced_memory()
            prev = rows
            peak_total += peak - before
            net_total += current - before
    finally:
        del prev
        tracemalloc.stop()
        gc.enable()
    return peak_total, net_total


async def _run_async_sequential(spec: AsyncSpec, plan: Plan, out: list[Result]) -> None:
    driver = spec.factory()
    await driver.connect(plan.dsn)
    try:
        await verify_async(spec, driver)
        for scenario in SEQUENTIAL:
            if scenario not in plan.scenarios:
                continue
            n = ROWS_PER_OP[scenario]
            logger.info("%-22s %s", spec.name, scenario)
            timing = await time_async(_async_op(driver, n), id_stream(plan.seed, n), plan)
            out.append(_timing_result(spec, "async", scenario, n, timing))
        if Scenario.ALLOC in plan.scenarios:
            logger.info("%-22s %s", spec.name, Scenario.ALLOC)
            for case in ALLOC_CASES:
                n = case[1]
                totals = await alloc_async(_async_op(driver, n), id_stream(plan.seed, n), plan)
                out.append(_alloc_result(spec, "async", case, plan, totals))
    finally:
        await driver.close()


async def _run_async_concurrent(spec: AsyncSpec, plan: Plan, out: list[Result]) -> None:
    """Every task shares the driver's own pool (``create_pool``), as an application would."""
    logger.info(
        "%-22s %s (x%d, pool %d)",
        spec.name,
        Scenario.CONCURRENT,
        plan.concurrency,
        plan.pool_size,
    )
    pool: AsyncPool = await spec.factory().create_pool(plan.dsn, plan.pool_size)
    try:
        _check_one(spec, 14, await pool.fetch_one(14))
        ops = [pool.fetch_one] * plan.concurrency
        timing = await time_concurrent(ops, id_stream(plan.seed, 1), plan)
        out.append(_timing_result(spec, "async", Scenario.CONCURRENT, 1, timing))
    finally:
        await pool.close()


async def _run_async_pinned(spec: AsyncSpec, plan: Plan, out: list[Result]) -> None:
    """Task ``k`` owns adapter ``k`` (one connection) for the whole run: no pool, no sharing."""
    logger.info("%-22s %s (x%d)", spec.name, Scenario.CONCURRENT_PINNED, plan.concurrency)
    drivers: list[AsyncDriver] = []
    try:
        for _ in range(plan.concurrency):
            driver = spec.factory()
            await driver.connect(plan.dsn)
            drivers.append(driver)
        _check_one(spec, 14, await drivers[0].fetch_one(14))
        ops = [driver.fetch_one for driver in drivers]
        timing = await time_concurrent(ops, id_stream(plan.seed, 1), plan)
        out.append(_timing_result(spec, "async", Scenario.CONCURRENT_PINNED, 1, timing))
    finally:
        for driver in drivers:
            await driver.close()


async def run_async_driver(spec: AsyncSpec, plan: Plan, out: list[Result]) -> None:
    """Run every selected scenario for one async adapter, appending to ``out`` as it goes."""
    if any(s in plan.scenarios for s in (*SEQUENTIAL, Scenario.ALLOC)):
        await _run_async_sequential(spec, plan, out)
    if Scenario.CONCURRENT_PINNED in plan.scenarios:
        await _run_async_pinned(spec, plan, out)
    if Scenario.CONCURRENT in plan.scenarios:
        await _run_async_concurrent(spec, plan, out)
