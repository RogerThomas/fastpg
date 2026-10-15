#!yeet
"""Single-row writes: latency and CPU per ``INSERT``, one request at a time, per driver.

Each driver inserts people-shaped rows into its own ``TEMP`` table (``LIKE people``, so the
primary key index is there too), one statement per request, in autocommit mode: plain
``INSERT`` (``execute``) and ``INSERT ... RETURNING id`` (a fetch of one value). Rounds
are interleaved across drivers, and each round truncates the table first. Reported per
method, as the median over rounds: p50/p99 latency, process CPU per request (every
thread), event-loop thread CPU per request and requests/s.

    uv run yeet ./bench/write_latency.py [-n 5000] [--repeat 5] [-m 'fastpg*']
"""

import fnmatch
import gc
import statistics
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, ClassVar, LiteralString, Protocol

import asyncpg
import msgspec
import psycopg
from drivers.base import DEFAULT_DSN, expected_person
from harness import run_coroutine
from rich.console import Console
from rich.table import Table
from yeetr import Opt

import fastpg

if TYPE_CHECKING:
    from asyncpg import Record


class SQL:
    """Statements shared by the methods (each connection has its own TEMP table)."""

    create: ClassVar[LiteralString] = (
        "CREATE TEMP TABLE bench_write_latency (LIKE people INCLUDING ALL)"
    )
    truncate: ClassVar[LiteralString] = "TRUNCATE bench_write_latency"
    insert_dollar: ClassVar[str] = (
        "INSERT INTO bench_write_latency VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)"
    )
    returning_dollar: ClassVar[str] = insert_dollar + " RETURNING id"
    insert_percent: ClassVar[LiteralString] = (
        "INSERT INTO bench_write_latency VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
    )
    returning_percent: ClassVar[LiteralString] = insert_percent + " RETURNING id"


@dataclass(slots=True)
class Round:
    method: str
    latencies_us: list[float]
    wall: float
    process_cpu: float
    loop_cpu: float


class WriteMethod(Protocol):
    name: ClassVar[str]

    async def open(self, dsn: str) -> None: ...

    async def truncate(self) -> None: ...

    async def insert(self, row: tuple[object, ...]) -> None: ...

    async def close(self) -> None: ...


@dataclass
class FastpgMethod:
    _conn: fastpg.Connection | None = field(default=None, init=False)

    def _connection(self) -> fastpg.Connection:
        assert self._conn is not None  # noqa: S101 - opened first
        return self._conn

    async def open(self, dsn: str) -> None:
        self._conn = await fastpg.connect(dsn)
        await self._conn.execute_script(SQL.create)

    async def truncate(self) -> None:
        await self._connection().execute_script(SQL.truncate)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()


class FastpgExecute(FastpgMethod):
    name: ClassVar[str] = "fastpg execute"

    async def insert(self, row: tuple[object, ...]) -> None:
        await self._connection().execute(SQL.insert_dollar, *row)


class FastpgReturning(FastpgMethod):
    name: ClassVar[str] = "fastpg select_one (RETURNING)"

    async def insert(self, row: tuple[object, ...]) -> None:
        await self._connection().select_one(SQL.returning_dollar, *row)


@dataclass
class AsyncpgMethod:
    _conn: asyncpg.Connection[Record] | None = field(default=None, init=False)

    def _connection(self) -> asyncpg.Connection[Record]:
        assert self._conn is not None  # noqa: S101 - opened first
        return self._conn

    async def open(self, dsn: str) -> None:
        self._conn = await asyncpg.connect(dsn)
        await self._conn.execute(SQL.create)

    async def truncate(self) -> None:
        await self._connection().execute(SQL.truncate)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()


class AsyncpgExecute(AsyncpgMethod):
    name: ClassVar[str] = "asyncpg execute"

    async def insert(self, row: tuple[object, ...]) -> None:
        await self._connection().execute(SQL.insert_dollar, *row)


class AsyncpgReturning(AsyncpgMethod):
    name: ClassVar[str] = "asyncpg fetchval (RETURNING)"

    async def insert(self, row: tuple[object, ...]) -> None:
        await self._connection().fetchval(SQL.returning_dollar, *row)


@dataclass
class PsycopgMethod:
    """psycopg 3 async in autocommit mode (one implicit transaction per statement)."""

    _conn: psycopg.AsyncConnection | None = field(default=None, init=False)

    def _connection(self) -> psycopg.AsyncConnection:
        assert self._conn is not None  # noqa: S101 - opened first
        return self._conn

    async def open(self, dsn: str) -> None:
        self._conn = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
        await self._conn.execute(SQL.create)

    async def truncate(self) -> None:
        await self._connection().execute(SQL.truncate)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()


class PsycopgExecute(PsycopgMethod):
    name: ClassVar[str] = "psycopg execute"

    async def insert(self, row: tuple[object, ...]) -> None:
        await self._connection().execute(SQL.insert_percent, row)


class PsycopgReturning(PsycopgMethod):
    name: ClassVar[str] = "psycopg execute+fetchone (RETURNING)"

    async def insert(self, row: tuple[object, ...]) -> None:
        cur = await self._connection().execute(SQL.returning_percent, row)
        await cur.fetchone()


class Methods:
    """Every benchmarked method, in report order."""

    all: ClassVar[tuple[type[WriteMethod], ...]] = (
        FastpgExecute,
        FastpgReturning,
        AsyncpgExecute,
        AsyncpgReturning,
        PsycopgExecute,
        PsycopgReturning,
    )


async def measure(method: WriteMethod, rows: list[tuple[object, ...]]) -> Round:
    await method.truncate()
    gc.collect()
    latencies: list[float] = []
    p0, c0, w0 = time.process_time(), time.thread_time(), time.perf_counter()
    for row in rows:
        start = time.perf_counter_ns()
        await method.insert(row)
        latencies.append((time.perf_counter_ns() - start) / 1e3)
    wall = time.perf_counter() - w0
    return Round(
        method.name,
        latencies,
        wall,
        time.process_time() - p0,
        time.thread_time() - c0,
    )


def summary_row(rounds: list[Round]) -> list[str]:
    n = len(rounds[0].latencies_us)
    p50 = [statistics.median(r.latencies_us) for r in rounds]
    p99 = [sorted(r.latencies_us)[int(n * 0.99)] for r in rounds]
    rates = sorted(n / r.wall for r in rounds)
    return [
        rounds[0].method,
        f"{statistics.median(p50):,.1f}",
        f"{statistics.median(p99):,.1f}",
        f"{statistics.median(r.process_cpu for r in rounds) * 1e6 / n:,.1f}",
        f"{statistics.median(r.loop_cpu for r in rounds) * 1e6 / n:,.1f}",
        f"{statistics.median(rates):,.0f}",
        f"{rates[0]:,.0f}-{rates[-1]:,.0f}",
    ]


async def run(methods: list[WriteMethod], dsn: str, requests: int, repeat: int) -> list[Round]:
    rows = [msgspec.structs.astuple(expected_person(i)) for i in range(1, requests + 1)]
    rounds: list[Round] = []
    try:
        for method in methods:
            await method.open(dsn)
            await measure(method, rows[: min(requests, 500)])  # warm-up
        for _ in range(repeat):
            for method in methods:
                rounds.append(await measure(method, rows))
    finally:
        for method in methods:
            await method.close()
    return rounds


def report(rounds: list[Round], requests: int, repeat: int) -> Table:
    title = f"Single-row INSERT, sequential ({requests:,} requests, median of {repeat} rounds)"
    table = Table(title=title)
    table.add_column("method", no_wrap=True)
    for col in ("p50 µs", "p99 µs", "process CPU µs/req", "loop CPU µs/req", "req/s", "min-max"):
        table.add_column(col, justify="right", no_wrap=True)
    groups: dict[str, list[Round]] = {}
    for r in rounds:
        groups.setdefault(r.method, []).append(r)
    for group in groups.values():
        table.add_row(*summary_row(group))
    return table


def main(
    *,
    requests: Annotated[int, Opt("-n")] = 5000,
    repeat: Annotated[int, Opt()] = 5,
    methods: Annotated[list[str], Opt("-m")] = ["*"],  # noqa: B006 - read only
    dsn: Annotated[str, Opt(envvar="FASTPG_DSN", help="Database URL")] = DEFAULT_DSN,
) -> None:
    """Benchmark single-row INSERTs.

    Args:
        requests: Measured requests per round.
        repeat: Measured rounds per method (interleaved); medians are over these.
        methods: Method names or globs (e.g. 'fastpg*', '*RETURNING*').
        dsn: PostgreSQL DSN.
    """
    selected = [m() for m in Methods.all if any(fnmatch.fnmatch(m.name, p) for p in methods)]
    if not selected:
        msg = f"no methods match {methods}"
        raise SystemExit(msg)
    rounds = run_coroutine(run(selected, dsn, requests, repeat))
    Console(width=160).print(report(rounds, requests, repeat))
