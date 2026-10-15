#!yeet
"""Bulk inserts: fastpg vs asyncpg vs psycopg 3 (async), into a fresh ``people``-shaped table.

Every round runs each (method, size) once, in the same order, so a noisy neighbour on a
shared host hits all methods alike; an unmeasured warm-up round comes first. For each run,
the table is truncated, then one call writes all rows. Reported per (method, size), over
the ``--repeat`` rounds: the median rows/s with its min-max spread, the median CPU of the
event-loop thread per row (``time.thread_time``, the application's own thread) and the
median CPU of the whole process (every thread included). Rows are built before timing, in
every shape a method takes: msgspec ``Person`` structs, tuples, dicts and pydantic models.

The table is created next to ``people`` (``LIKE people INCLUDING ALL``) under a name unique
to this process, and dropped at the end; ``people`` itself is only read.

    uv run yeet ./bench/writes.py [-r 1000 -r 10000 -r 100000] [-m 'fastpg*'] [--repeat 5]
    uv run yeet ./bench/writes.py -m '*copy*' --json /tmp/copy.json   # raw runs as JSON
"""

import datetime as dt
import decimal
import fnmatch
import gc
import os
import statistics
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, ClassVar, Protocol

import asyncpg
import msgspec
import psycopg
from drivers.base import DEFAULT_DSN, Person, PersonModel
from harness import run_coroutine
from psycopg import sql
from rich.console import Console
from rich.table import Table
from yeetr import Opt

import fastpg

if TYPE_CHECKING:
    from asyncpg import Record

type PersonRow = tuple[
    int,
    str,
    int,
    str | None,
    float,
    decimal.Decimal,
    bool,
    uuid.UUID,
    dt.datetime,
    dt.date | None,
]


@dataclass(slots=True)
class Batch:
    """The same rows in every shape."""

    people: list[Person]
    tuples: list[PersonRow]
    dicts: list[dict[str, object]]
    models: list[PersonModel]


@dataclass(slots=True)
class Measurement:
    method: str
    rows: int
    wall: float
    loop_cpu: float
    process_cpu: float


@dataclass(frozen=True, slots=True)
class Target:
    """The benchmark's table and the statements that write to it."""

    table: str
    columns: ClassVar[tuple[str, ...]] = (
        "id",
        "name",
        "age",
        "email",
        "score",
        "balance",
        "active",
        "external_id",
        "created_at",
        "birthday",
    )
    pg_types: ClassVar[list[str]] = [
        "int4",
        "text",
        "int4",
        "text",
        "float8",
        "numeric",
        "bool",
        "uuid",
        "timestamptz",
        "date",
    ]

    @property
    def quoted(self) -> str:
        return fastpg.Identifier(self.table).quoted

    @property
    def create(self) -> str:
        return (
            f"DROP TABLE IF EXISTS {self.quoted}; "
            f"CREATE TABLE {self.quoted} (LIKE people INCLUDING ALL)"
        )

    @property
    def insert_dollar(self) -> str:
        return f"INSERT INTO {self.quoted} VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)"  # noqa: S608 - quoted

    @property
    def insert_percent(self) -> sql.Composed:
        return sql.SQL("INSERT INTO {} VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)").format(
            sql.Identifier(self.table),
        )

    @property
    def copy_binary(self) -> sql.Composed:
        return sql.SQL("COPY {} FROM STDIN (FORMAT BINARY)").format(sql.Identifier(self.table))


class WriteMethod(Protocol):
    name: ClassVar[str]

    async def open(self, dsn: str) -> None: ...

    async def write(self, target: Target, batch: Batch) -> None: ...

    async def close(self) -> None: ...


@dataclass
class FastpgMethod:
    _conn: fastpg.Connection | None = field(default=None, init=False)

    def _connection(self) -> fastpg.Connection:
        assert self._conn is not None  # noqa: S101 - opened first
        return self._conn

    async def open(self, dsn: str) -> None:
        self._conn = await fastpg.connect(dsn)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()


class FastpgExecuteMany(FastpgMethod):
    name: ClassVar[str] = "fastpg execute_many (tuples)"

    async def write(self, target: Target, batch: Batch) -> None:
        await self._connection().execute_many(target.insert_dollar, batch.tuples)


class FastpgInsertMany(FastpgMethod):
    name: ClassVar[str] = "fastpg insert_many (Structs)"

    async def write(self, target: Target, batch: Batch) -> None:
        await self._connection().insert_many(target.table, batch.people)


class FastpgInsertManyTuples(FastpgMethod):
    name: ClassVar[str] = "fastpg insert_many (tuples)"

    async def write(self, target: Target, batch: Batch) -> None:
        await self._connection().insert_many(target.table, batch.tuples)


class FastpgInsertManyDicts(FastpgMethod):
    name: ClassVar[str] = "fastpg insert_many (dicts)"

    async def write(self, target: Target, batch: Batch) -> None:
        await self._connection().insert_many(target.table, batch.dicts)


class FastpgCOPYStructs(FastpgMethod):
    name: ClassVar[str] = "fastpg copy_in (Structs)"

    async def write(self, target: Target, batch: Batch) -> None:
        await self._connection().copy_in(target.table, batch.people)


class FastpgCOPYTuples(FastpgMethod):
    name: ClassVar[str] = "fastpg copy_in (tuples)"

    async def write(self, target: Target, batch: Batch) -> None:
        await self._connection().copy_in(target.table, batch.tuples)


class FastpgCOPYDicts(FastpgMethod):
    name: ClassVar[str] = "fastpg copy_in (dicts)"

    async def write(self, target: Target, batch: Batch) -> None:
        await self._connection().copy_in(target.table, batch.dicts)


class FastpgCOPYModels(FastpgMethod):
    name: ClassVar[str] = "fastpg copy_in (pydantic)"

    async def write(self, target: Target, batch: Batch) -> None:
        await self._connection().copy_in(target.table, batch.models)


@dataclass
class AsyncpgMethod:
    _conn: asyncpg.Connection[Record] | None = field(default=None, init=False)

    def _connection(self) -> asyncpg.Connection[Record]:
        assert self._conn is not None  # noqa: S101 - opened first
        return self._conn

    async def open(self, dsn: str) -> None:
        self._conn = await asyncpg.connect(dsn)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()


class AsyncpgExecuteMany(AsyncpgMethod):
    name: ClassVar[str] = "asyncpg executemany"

    async def write(self, target: Target, batch: Batch) -> None:
        await self._connection().executemany(target.insert_dollar, batch.tuples)


class AsyncpgCOPY(AsyncpgMethod):
    name: ClassVar[str] = "asyncpg copy_records_to_table"

    async def write(self, target: Target, batch: Batch) -> None:
        await self._connection().copy_records_to_table(
            target.table,
            records=batch.tuples,
            columns=Target.columns,
        )


@dataclass
class PsycopgMethod:
    """psycopg 3 async, default settings (autocommit off: one transaction per call)."""

    _conn: psycopg.AsyncConnection | None = field(default=None, init=False)

    def _connection(self) -> psycopg.AsyncConnection:
        assert self._conn is not None  # noqa: S101 - opened first
        return self._conn

    async def open(self, dsn: str) -> None:
        self._conn = await psycopg.AsyncConnection.connect(dsn)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()


class PsycopgExecuteMany(PsycopgMethod):
    name: ClassVar[str] = "psycopg executemany"

    async def write(self, target: Target, batch: Batch) -> None:
        conn = self._connection()
        async with conn.cursor() as cur:
            await cur.executemany(target.insert_percent, batch.tuples)
        await conn.commit()


class PsycopgCOPY(PsycopgMethod):
    name: ClassVar[str] = "psycopg copy (binary)"

    async def write(self, target: Target, batch: Batch) -> None:
        conn = self._connection()
        async with conn.cursor() as cur, cur.copy(target.copy_binary) as copy:
            copy.set_types(Target.pg_types)
            for row in batch.tuples:
                await copy.write_row(row)
        await conn.commit()


class Methods:
    """Every benchmarked method, in report order."""

    all: ClassVar[tuple[type[WriteMethod], ...]] = (
        FastpgExecuteMany,
        FastpgInsertMany,
        FastpgInsertManyTuples,
        FastpgInsertManyDicts,
        FastpgCOPYStructs,
        FastpgCOPYTuples,
        FastpgCOPYDicts,
        FastpgCOPYModels,
        AsyncpgExecuteMany,
        AsyncpgCOPY,
        PsycopgExecuteMany,
        PsycopgCOPY,
    )


def make_row(i: int) -> PersonRow:
    """Deterministic people-shaped row (same formulas as the seed data)."""
    return (
        i,
        f"person-{i}",
        18 + i % 60,
        None if i % 7 == 0 else f"person{i}@example.com",
        (i % 1000) / 10,
        decimal.Decimal((i * 37) % 100_000) / 100,
        i % 2 == 0,
        uuid.UUID(int=i),
        dt.datetime(2024, 1, 1, tzinfo=dt.UTC) + dt.timedelta(seconds=i * 61),
        None if i % 11 == 0 else dt.date(1950, 1, 1) + dt.timedelta(days=i % 20_000),
    )


def make_batch(rows: int) -> Batch:
    tuples = [make_row(i) for i in range(1, rows + 1)]
    people = [Person(*row) for row in tuples]
    dicts = [msgspec.structs.asdict(p) for p in people]
    models = [PersonModel.model_construct(**d) for d in dicts]
    return Batch(people, tuples, dicts, models)


async def measure(
    method: WriteMethod,
    control: fastpg.Connection,
    target: Target,
    batch: Batch,
) -> Measurement:
    await control.execute_script(f"TRUNCATE {target.quoted}")
    gc.collect()
    w0, c0, p0 = time.perf_counter(), time.thread_time(), time.process_time()
    await method.write(target, batch)
    wall = time.perf_counter() - w0
    loop_cpu = time.thread_time() - c0
    process_cpu = time.process_time() - p0
    (count,) = await control.select_one(f"SELECT count(*) FROM {target.quoted}")  # noqa: S608
    if count != len(batch.tuples):
        msg = f"{method.name}: wrote {count} rows, expected {len(batch.tuples)}"
        raise RuntimeError(msg)
    return Measurement(method.name, len(batch.tuples), wall, loop_cpu, process_cpu)


def summary_row(runs: list[Measurement]) -> list[str]:
    rows = runs[0].rows
    rates = sorted(rows / m.wall for m in runs)
    return [
        runs[0].method,
        f"{rows:,}",
        f"{statistics.median(rates):,.0f}",
        f"{rates[0]:,.0f}-{rates[-1]:,.0f}",
        f"{statistics.median(m.wall for m in runs) * 1e3:,.1f}",
        f"{statistics.median(m.loop_cpu for m in runs) * 1e9 / rows:,.0f}",
        f"{statistics.median(m.process_cpu for m in runs) * 1e3:,.1f}",
    ]


async def run(
    methods: list[WriteMethod],
    dsn: str,
    target: Target,
    sizes: list[int],
    repeat: int,
) -> list[Measurement]:
    batches = {rows: make_batch(rows) for rows in sizes}
    runs: list[Measurement] = []
    async with await fastpg.connect(dsn) as control:
        await control.execute_script(target.create)
        try:
            for method in methods:
                await method.open(dsn)
            for method in methods:  # warm-up: type introspection, statement caches
                await measure(method, control, target, batches[min(sizes)])
            for _ in range(repeat):
                for method in methods:
                    for rows in sizes:
                        runs.append(await measure(method, control, target, batches[rows]))
        finally:
            for method in methods:
                await method.close()
            await control.execute_script(f"DROP TABLE IF EXISTS {target.quoted}")
    return runs


def report(runs: list[Measurement], repeat: int) -> Table:
    table = Table(title=f"Bulk insert into a fresh people-shaped table (median of {repeat})")
    table.add_column("method", no_wrap=True)
    for col in ("rows", "rows/s", "rows/s min-max", "wall ms", "loop ns/row", "process CPU ms"):
        table.add_column(col, justify="right", no_wrap=True)
    groups: dict[tuple[str, int], list[Measurement]] = {}
    for m in runs:
        groups.setdefault((m.method, m.rows), []).append(m)
    previous = None
    for (method, _), group in groups.items():
        if previous is not None and method != previous:
            table.add_section()
        table.add_row(*summary_row(group))
        previous = method
    return table


def main(
    *,
    rows: Annotated[list[int], Opt("-r")] = [1_000, 10_000, 100_000],  # noqa: B006 - read only
    methods: Annotated[list[str], Opt("-m")] = ["*"],  # noqa: B006 - read only
    repeat: Annotated[int, Opt()] = 5,
    table: Annotated[str, Opt()] = "",
    json: Annotated[str, Opt()] = "",
    dsn: Annotated[str, Opt(envvar="FASTPG_DSN", help="Database URL")] = DEFAULT_DSN,
) -> None:
    """Benchmark bulk inserts.

    Args:
        rows: Rows written per call.
        methods: Method names or globs (e.g. 'fastpg*', 'asyncpg*', '*copy*').
        repeat: Measured rounds; medians and min-max are over these.
        table: Table to create (and drop) next to ``people``; default ``bench_writes_<pid>``.
        json: Also write every measured run to this file, as JSON.
        dsn: PostgreSQL DSN.
    """
    selected = [m() for m in Methods.all if any(fnmatch.fnmatch(m.name, p) for p in methods)]
    if not selected:
        msg = f"no methods match {methods}"
        raise SystemExit(msg)
    target = Target(table or f"bench_writes_{os.getpid()}")
    runs = run_coroutine(run(selected, dsn, target, rows, repeat))
    if json:
        Path(json).write_bytes(msgspec.json.encode(runs))
    Console(width=160).print(report(runs, repeat))
