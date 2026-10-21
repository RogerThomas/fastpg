"""Timing and trial orchestration.

One *run* is one (trial, workload, driver configuration): open a fresh pool, check the
driver's results (reads), warm up at the workload's own concurrency, measure, check what was
written (writes), close the pool. Trials repeat every run; within each trial the
configurations of a workload run back to back in a freshly shuffled (seeded) order, so a
noisy neighbour, CPU frequency drift or table growth can't systematically favour whichever
driver always runs first or last.

Closed loop: ``concurrency`` tasks issue ops back to back. Throughput is measured ops over
the wall time of the measured phase; latency is per op (``perf_counter_ns``). Process CPU
(every thread: psqlpy's tokio threads included) and loop-thread CPU are recorded too.

Open loop: requests arrive at a fixed rate (seeded Poisson process), each as its own task
on the pool. Latency counts from the *scheduled* arrival, so a driver that holds the event
loop and delays later arrivals pays for it (no coordinated omission); the service part
(dispatch to result) is reported separately. A rate whose outstanding requests exceed
``max_inflight`` is marked overloaded and stopped.
"""

import asyncio
import gc
import logging
import random
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

import msgspec
from psycopg import AsyncConnection

from .data import Expected, MismatchError, Schema
from .workloads import OpenLoop, Streams

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .adapters import Adapter, Config
    from .workloads import Op, Workload

logger = logging.getLogger("bench")


@dataclass(frozen=True, slots=True, kw_only=True)
class Plan:
    dsn: str
    trials: int
    duration_s: float
    warmup_s: float
    pool_size: int
    seed: int
    max_inflight: int
    keep_schema: bool

    def cell_timeout(self) -> float:
        """Upper bound on one run, so a hung driver fails the run instead of the job."""
        return 2 * (self.warmup_s + self.duration_s) + 120


class Run(msgspec.Struct, kw_only=True, omit_defaults=True):
    """One measurement. Latencies in µs; CPU in µs per op."""

    trial: int
    workload: str
    config: str
    position: int  # 0-based slot in this trial's shuffled order for the workload
    ops: int = 0
    seconds: float = 0.0
    ops_per_s: float | None = None
    rows_per_s: float | None = None
    p50_us: float | None = None
    p90_us: float | None = None
    p99_us: float | None = None
    p999_us: float | None = None
    max_us: float | None = None
    cpu_us_per_op: float | None = None
    loop_cpu_us_per_op: float | None = None
    # open loop only
    target_rps: int | None = None
    achieved_rps: float | None = None
    service_p50_us: float | None = None
    service_p99_us: float | None = None
    loop_busy_pct: float | None = None
    overloaded: bool | None = None
    error_kind: Literal["mismatch", "crash"] | None = None
    error: str | None = None


@dataclass(slots=True)
class Timing:
    latencies_ns: list[int]
    elapsed_ns: int
    cpu_s: float
    loop_cpu_s: float


def _pct(sorted_ns: Sequence[int], q: float) -> float:
    """Nearest-rank percentile, in µs."""
    return round(sorted_ns[min(len(sorted_ns) - 1, int(q * len(sorted_ns)))] / 1e3, 1)


@dataclass(slots=True)
class ClosedLoop:
    """``tasks`` tasks running ``op`` back to back: a warm-up phase, then a measured one."""

    op: Op
    tasks: int
    max_ops: int | None
    completed: int = 0  # every op, warm-up included (the write checks need the total)
    _started: int = 0
    _latencies: list[int] = field(default_factory=list[int])
    _stride: int = 0

    def _more(self, now_ns: int, deadline_ns: int, cap: int | None) -> bool:
        return now_ns < deadline_ns and (cap is None or self._started < cap)

    async def _warm(self, k: int, end_ns: int, cap: int | None) -> None:
        i = k * self._stride
        n = 0
        # At least 3 ops per task, and until the warm-up deadline (or the bulk cap).
        while n < 3 or self._more(time.perf_counter_ns(), end_ns, cap):
            self._started += 1
            await self.op(i)
            self.completed += 1
            i += 1
            n += 1

    async def _measure(self, k: int, deadline_ns: int) -> None:
        i = k * self._stride + self._stride // 2  # don't replay this task's warm-up args
        lat = self._latencies
        now = time.perf_counter_ns()
        while self._more(now, deadline_ns, self.max_ops):
            self._started += 1
            t0 = time.perf_counter_ns()
            await self.op(i)
            now = time.perf_counter_ns()
            lat.append(now - t0)
            self.completed += 1
            i += 1

    async def run(self, warmup_s: float, duration_s: float) -> Timing:
        self._stride = (1 << 30) // self.tasks
        warm_cap = None if self.max_ops is None else max(self.tasks * 3, self.max_ops // 10)
        warm_end = time.perf_counter_ns() + int(warmup_s * 1e9)
        async with asyncio.TaskGroup() as tg:
            for k in range(self.tasks):
                tg.create_task(self._warm(k, warm_end, warm_cap))
        gc.collect()
        self._started = 0
        cpu0, loop0 = time.process_time(), time.thread_time()
        start = time.perf_counter_ns()
        deadline = start + int(duration_s * 1e9)
        async with asyncio.TaskGroup() as tg:
            for k in range(self.tasks):
                tg.create_task(self._measure(k, deadline))
        elapsed = time.perf_counter_ns() - start
        return Timing(
            self._latencies,
            elapsed,
            time.process_time() - cpu0,
            time.thread_time() - loop0,
        )


@dataclass(slots=True)
class _Arrivals:
    """Bookkeeping for one open-loop phase."""

    adapter: Adapter
    ids: list[int]
    record: bool
    response_ns: list[int] = field(default_factory=list[int])
    service_ns: list[int] = field(default_factory=list[int])
    inflight: int = 0
    sent: int = 0
    errors: list[Exception] = field(default_factory=list[Exception])
    _idle: asyncio.Event = field(default_factory=asyncio.Event)
    _tasks: set[asyncio.Task[None]] = field(default_factory=set[asyncio.Task[None]])

    async def _request(self, scheduled_ns: int, dispatched_ns: int, person_id: int) -> None:
        try:
            await self.adapter.person(person_id)
        except Exception as exc:  # noqa: BLE001 - re-raised by drain(), once the phase ends
            self.errors.append(exc)
        finally:
            done = time.perf_counter_ns()
            if self.record:
                self.response_ns.append(done - scheduled_ns)
                self.service_ns.append(done - dispatched_ns)
            self.inflight -= 1
            if self.inflight == 0:
                self._idle.set()

    def dispatch(self, scheduled_ns: int, now_ns: int) -> None:
        person_id = self.ids[self.sent & (len(self.ids) - 1)]
        self.inflight += 1
        self.sent += 1
        self._idle.clear()
        task = asyncio.get_running_loop().create_task(
            self._request(scheduled_ns, now_ns, person_id),
        )
        self._tasks.add(task)  # strong reference until done (the loop keeps a weak one)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        if self.inflight:
            await self._idle.wait()
        if self.errors:
            raise self.errors[0]


@dataclass(slots=True)
class OpenLoopPhase:
    """Poisson arrivals at ``rate``/s for ``seconds``; returns (overloaded, wall_ns, loop CPU)."""

    arrivals: _Arrivals
    rate: int
    seconds: float
    max_inflight: int
    rng: random.Random

    async def run(self) -> tuple[bool, int, float]:
        start = time.perf_counter_ns()
        cpu0 = time.thread_time()
        end = start + int(self.seconds * 1e9)
        next_ns = start
        overloaded = False
        while next_ns < end and not overloaded:
            now = time.perf_counter_ns()
            if next_ns > now:
                await asyncio.sleep((next_ns - now) / 1e9)
                now = time.perf_counter_ns()
            while next_ns <= now and next_ns < end:
                self.arrivals.dispatch(next_ns, now)
                next_ns += int(self.rng.expovariate(self.rate) * 1e9)
            overloaded = self.arrivals.inflight > self.max_inflight
            await asyncio.sleep(0)
        await self.arrivals.drain()
        return overloaded, time.perf_counter_ns() - start, time.thread_time() - cpu0


def _closed_result(base: Run, workload: Workload, timing: Timing) -> Run:
    lat = sorted(timing.latencies_ns)
    ops = len(lat)
    seconds = timing.elapsed_ns / 1e9
    ops_per_s = ops / seconds
    return msgspec.structs.replace(
        base,
        ops=ops,
        seconds=round(seconds, 3),
        ops_per_s=round(ops_per_s, 1),
        rows_per_s=round(ops_per_s * workload.rows_per_op, 1),
        p50_us=_pct(lat, 0.50),
        p90_us=_pct(lat, 0.90),
        p99_us=_pct(lat, 0.99),
        p999_us=_pct(lat, 0.999),
        max_us=round(lat[-1] / 1e3, 1),
        cpu_us_per_op=round(timing.cpu_s * 1e6 / ops, 2),
        loop_cpu_us_per_op=round(timing.loop_cpu_s * 1e6 / ops, 2),
    )


async def _closed(
    base: Run,
    workload: Workload,
    adapter: Adapter,
    plan: Plan,
    control: AsyncConnection,
) -> Run:
    snapshot = await workload.prepare(control)
    await workload.precheck(adapter)
    loop = ClosedLoop(workload.op(adapter, plan.seed), workload.concurrency, workload.max_ops)
    timing = await loop.run(plan.warmup_s, plan.duration_s)
    if not timing.latencies_ns:
        msg = f"{workload.name}: no op completed in {plan.duration_s}s"
        raise MismatchError(msg)
    await workload.postcheck(control, snapshot, loop.completed)
    return _closed_result(base, workload, timing)


async def _open(base: Run, workload: OpenLoop, adapter: Adapter, plan: Plan) -> Run:
    await workload.precheck(adapter)
    rng = random.Random(f"{plan.seed}:{workload.name}")
    ids = Streams.ints(plan.seed, workload.name, 1, Expected.people_rows)
    warm = _Arrivals(adapter, ids, record=False)
    await OpenLoopPhase(warm, workload.rate, plan.warmup_s, plan.max_inflight, rng).run()
    gc.collect()
    arrivals = _Arrivals(adapter, ids, record=True)
    phase = OpenLoopPhase(arrivals, workload.rate, plan.duration_s, plan.max_inflight, rng)
    overloaded, wall_ns, loop_cpu = await phase.run()
    response = sorted(arrivals.response_ns)
    service = sorted(arrivals.service_ns)
    if not response:
        msg = f"{workload.name}: no request completed"
        raise MismatchError(msg)
    return msgspec.structs.replace(
        base,
        ops=len(response),
        seconds=round(wall_ns / 1e9, 3),
        target_rps=workload.rate,
        achieved_rps=round(arrivals.sent / (wall_ns / 1e9), 1),
        p50_us=_pct(response, 0.50),
        p90_us=_pct(response, 0.90),
        p99_us=_pct(response, 0.99),
        p999_us=_pct(response, 0.999),
        max_us=round(response[-1] / 1e3, 1),
        service_p50_us=_pct(service, 0.50),
        service_p99_us=_pct(service, 0.99),
        loop_busy_pct=round(100 * loop_cpu / (wall_ns / 1e9), 1),
        overloaded=overloaded,
    )


async def _measure(
    base: Run,
    workload: Workload,
    config: Config,
    plan: Plan,
    control: AsyncConnection,
) -> Run:
    adapter = config.factory()
    await adapter.open(plan.dsn, plan.pool_size)
    try:
        if isinstance(workload, OpenLoop):
            return await _open(base, workload, adapter, plan)
        return await _closed(base, workload, adapter, plan, control)
    finally:
        await adapter.close()


async def _run_one(
    base: Run,
    workload: Workload,
    config: Config,
    plan: Plan,
    control: AsyncConnection,
) -> Run:
    gc.collect()
    try:
        async with asyncio.timeout(plan.cell_timeout()):
            return await _measure(base, workload, config, plan, control)
    except MismatchError as exc:
        logger.error("%s / %s: MISMATCH: %s", workload.name, config.name, exc)  # noqa: TRY400
        return msgspec.structs.replace(base, error_kind="mismatch", error=str(exc))
    except Exception as exc:
        logger.exception("%s / %s crashed", workload.name, config.name)
        error = f"{type(exc).__name__}: {exc}"
        return msgspec.structs.replace(base, error_kind="crash", error=error)


def applies(workload: Workload, config: Config) -> bool:
    """Whether ``config`` runs ``workload`` (see ``Config.shared_variant_of``)."""
    return not (workload.exclusive and config.shared_variant_of is not None)


def _describe(run: Run) -> str:
    if run.error is not None:
        return f"{run.error_kind}: {run.error}"
    if run.target_rps is not None:
        state = "OVERLOADED" if run.overloaded else f"{run.achieved_rps:,.0f}/s"
        return f"{state} p50 {run.p50_us:,.0f} µs p99 {run.p99_us:,.0f} µs"
    return f"{run.ops_per_s:,.0f} ops/s p50 {run.p50_us:,.0f} µs p99 {run.p99_us:,.0f} µs"


async def run_all(
    plan: Plan,
    workloads: Sequence[Workload],
    configs: Sequence[Config],
) -> list[Run]:
    """Seed the schema, then run every (trial, workload, configuration)."""
    runs: list[Run] = []
    warned = False
    async with await AsyncConnection.connect(plan.dsn, autocommit=True) as control:
        await Schema.create(control, keep=plan.keep_schema)
        for trial in range(plan.trials):
            for workload in workloads:
                chosen = [c for c in configs if applies(workload, c)]
                order = random.Random(f"{plan.seed}:{trial}:{workload.name}").sample(
                    chosen,
                    k=len(chosen),
                )
                for position, config in enumerate(order):
                    base = Run(
                        trial=trial,
                        workload=workload.name,
                        config=config.name,
                        position=position,
                    )
                    if not await Schema.settle(control) and not warned:
                        logger.warning("no CHECKPOINT between runs: role lacks pg_checkpoint")
                        warned = True
                    run = await _run_one(base, workload, config, plan, control)
                    logger.info(
                        "trial %d/%d %-22s %-17s %s",
                        trial + 1,
                        plan.trials,
                        workload.name,
                        config.name,
                        _describe(run),
                    )
                    runs.append(run)
    return runs
