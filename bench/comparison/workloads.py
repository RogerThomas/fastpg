"""The workloads: what each one runs, at what concurrency, and how its results are checked.

A workload builds an *op*, a callable taking a running index and returning the awaitable for
that operation; every driver configuration gets the identical sequence of arguments (seeded
per workload). Reads are checked before timing (``precheck``: every driver must return the
exact expected rows, in its own row type or as the shared Structs); writes are checked after
timing, server side, through the psycopg control connection (``postcheck``: row counts,
balance invariants and every written value against its formula).
"""

import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Literal, LiteralString

from .data import EventRow, Expected, MismatchError, Schema, check_rows

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    import msgspec
    from psycopg import AsyncConnection

    from .adapters import Adapter

type Op = Callable[[int], Awaitable[object]]
type Category = Literal["read", "write", "bulk", "open-loop"]


@dataclass(frozen=True, slots=True)
class Call:
    """``fn(*args[i])``, cycling through a pre-built argument stream (a power-of-two long)."""

    fn: Callable[..., Awaitable[object]]
    args: Sequence[tuple[object, ...]]
    _mask: int = field(init=False)

    def __post_init__(self) -> None:
        n = len(self.args)
        if n & (n - 1):
            msg = f"argument stream length must be a power of two, got {n}"
            raise ValueError(msg)
        object.__setattr__(self, "_mask", n - 1)

    def __call__(self, i: int) -> Awaitable[object]:
        return self.fn(*self.args[i & self._mask])


class Streams:
    """Deterministic argument streams: the same per workload for every driver and trial."""

    LENGTH: ClassVar[int] = 1 << 14

    @classmethod
    def ints(cls, seed: int, name: str, lo: int, hi: int) -> list[int]:
        rng = random.Random(f"{seed}:{name}")
        return [rng.randint(lo, hi) for _ in range(cls.LENGTH)]

    @classmethod
    def pairs(cls, seed: int, name: str, lo: int, hi: int) -> list[tuple[int, int]]:
        rng = random.Random(f"{seed}:{name}")
        return [(a, b) for a, b in (rng.sample(range(lo, hi + 1), 2) for _ in range(cls.LENGTH))]


async def _one_row(control: AsyncConnection, query: LiteralString) -> tuple[int, ...]:
    cur = await control.execute(query)
    row = await cur.fetchone()
    if row is None:
        msg = f"no row from {query!r}"
        raise MismatchError(msg)
    return tuple(int(v) for v in row)


@dataclass(frozen=True, slots=True, kw_only=True)
class Workload:
    """Base: a closed-loop workload (``concurrency`` tasks issuing ops back to back)."""

    name: str
    title: str
    why: str
    category: Category
    concurrency: int
    rows_per_op: int = 1
    typed: bool | None = None  # None: no rows returned
    exclusive: bool = False  # checks a connection out (transaction / bulk write)
    max_ops: int | None = None  # cap on measured ops (bulk writes), on top of the duration

    async def prepare(self, control: AsyncConnection) -> int:
        """Reset what this workload writes; return a snapshot for ``postcheck``."""
        del control
        return 0

    async def precheck(self, adapter: Adapter) -> None:
        """Raise MismatchError unless ``adapter`` returns exactly the expected data."""
        del adapter

    def op(self, adapter: Adapter, seed: int) -> Op:
        raise NotImplementedError

    async def postcheck(self, control: AsyncConnection, snapshot: int, ops: int) -> None:
        """Raise MismatchError unless the ``ops`` completed ops left the expected data."""
        del control, snapshot, ops


@dataclass(frozen=True, slots=True, kw_only=True)
class PointLookup(Workload):
    """One ``people`` row by primary key."""

    _sample: ClassVar[tuple[int, ...]] = (1, 7, 11, 77, 50_000, Expected.people_rows)

    def _fetch(self, adapter: Adapter) -> Callable[[int], Awaitable[object]]:
        return adapter.person_typed if self.typed else adapter.person

    async def precheck(self, adapter: Adapter) -> None:
        fetch = self._fetch(adapter)
        for person_id in self._sample:
            row = await fetch(person_id)
            want = [Expected.person(person_id)]
            check_rows(f"{self.name} id={person_id}", want, [row], typed=bool(self.typed))

    def op(self, adapter: Adapter, seed: int) -> Op:
        ids = Streams.ints(seed, self.name, 1, Expected.people_rows)
        return Call(self._fetch(adapter), [(i,) for i in ids])


@dataclass(frozen=True, slots=True, kw_only=True)
class RangeFetch(Workload):
    """``rows_per_op`` consecutive rows of ``people`` or ``wide``, in id order."""

    table: Literal["people", "wide"]

    def _table_rows(self) -> int:
        return Expected.people_rows if self.table == "people" else Expected.wide_rows

    def _fetch(self, adapter: Adapter) -> Callable[[int, int], Awaitable[Sequence[object]]]:
        if self.table == "people":
            return adapter.people_typed if self.typed else adapter.people
        return adapter.wide_typed if self.typed else adapter.wide

    def _expected(self, first: int) -> list[msgspec.Struct]:
        make = Expected.person if self.table == "people" else Expected.wide
        return [make(i) for i in range(first, first + self.rows_per_op)]

    async def precheck(self, adapter: Adapter) -> None:
        fetch = self._fetch(adapter)
        last_first = self._table_rows() - self.rows_per_op + 1
        for first in (1, last_first // 2, last_first):
            rows = await fetch(first, first + self.rows_per_op - 1)
            label = f"{self.name} ids {first}..{first + self.rows_per_op - 1}"
            check_rows(label, self._expected(first), rows, typed=bool(self.typed))

    def op(self, adapter: Adapter, seed: int) -> Op:
        n = self.rows_per_op
        starts = Streams.ints(seed, self.name, 1, self._table_rows() - n + 1)
        return Call(self._fetch(adapter), [(s, s + n - 1) for s in starts])


@dataclass(frozen=True, slots=True, kw_only=True)
class InsertOne(Workload):
    """INSERT one ``events`` row per op (autocommit)."""

    async def prepare(self, control: AsyncConnection) -> int:
        await control.execute(Schema.RESET_WRITES)
        return 0

    def op(self, adapter: Adapter, seed: int) -> Op:
        keys = Streams.ints(seed, self.name, 1, 1_000_000)
        return Call(adapter.insert_event, [(Expected.event(k),) for k in keys])

    async def postcheck(self, control: AsyncConnection, snapshot: int, ops: int) -> None:
        del snapshot
        rows, bad = await _one_row(control, Schema.EVENTS_OK)
        if rows != ops or bad:
            msg = f"{self.name}: {rows} rows ({bad} wrong) after {ops} inserts"
            raise MismatchError(msg)


@dataclass(frozen=True, slots=True, kw_only=True)
class UpdateOne(Workload):
    """UPDATE one ``accounts`` row by primary key per op (``balance + 1``, autocommit)."""

    async def prepare(self, control: AsyncConnection) -> int:
        await control.execute(Schema.VACUUM_ACCOUNTS)
        (total,) = await _one_row(control, Schema.BALANCE_TOTAL)
        return total

    def op(self, adapter: Adapter, seed: int) -> Op:
        ids = Streams.ints(seed, self.name, 1, Expected.accounts)
        return Call(adapter.add_balance, [(1, i) for i in ids])

    async def postcheck(self, control: AsyncConnection, snapshot: int, ops: int) -> None:
        (total,) = await _one_row(control, Schema.BALANCE_TOTAL)
        if total - snapshot != ops:
            msg = f"{self.name}: balances grew by {total - snapshot} after {ops} updates"
            raise MismatchError(msg)


@dataclass(frozen=True, slots=True, kw_only=True)
class Transfer(Workload):
    """A short transaction: lock two accounts, debit, credit, insert a ``transfers`` row."""

    async def prepare(self, control: AsyncConnection) -> int:
        await control.execute(Schema.RESET_WRITES)
        await control.execute(Schema.VACUUM_ACCOUNTS)
        (total,) = await _one_row(control, Schema.BALANCE_TOTAL)
        return total

    def op(self, adapter: Adapter, seed: int) -> Op:
        pairs = Streams.pairs(seed, self.name, 1, Expected.accounts)
        args = [(s, d, Expected.transfer_amount(s, d)) for s, d in pairs]
        return Call(adapter.transfer, args)

    async def postcheck(self, control: AsyncConnection, snapshot: int, ops: int) -> None:
        (total,) = await _one_row(control, Schema.BALANCE_TOTAL)
        rows, bad = await _one_row(control, Schema.TRANSFERS_OK)
        if total != snapshot or rows != ops or bad:
            msg = (
                f"{self.name}: total balance {snapshot} -> {total}, {rows} transfers "
                f"({bad} wrong) after {ops} transactions"
            )
            raise MismatchError(msg)


@dataclass(frozen=True, slots=True, kw_only=True)
class BulkInsert(Workload):
    """``rows_per_op`` rows per op into ``batch``, by executemany or by binary COPY."""

    method: Literal["executemany", "copy"]

    async def prepare(self, control: AsyncConnection) -> int:
        await control.execute(Schema.RESET_WRITES)
        return 0

    def op(self, adapter: Adapter, seed: int) -> Op:
        del seed
        batch: list[EventRow] = [Expected.event(k) for k in range(1, self.rows_per_op + 1)]
        fn = adapter.insert_batch if self.method == "executemany" else adapter.copy_batch
        return Call(fn, [(batch,)])

    async def postcheck(self, control: AsyncConnection, snapshot: int, ops: int) -> None:
        del snapshot
        rows, bad = await _one_row(control, Schema.BATCH_OK)
        if rows != ops * self.rows_per_op or bad:
            msg = f"{self.name}: {rows} rows ({bad} wrong) after {ops} x {self.rows_per_op}"
            raise MismatchError(msg)


@dataclass(frozen=True, slots=True, kw_only=True)
class OpenLoop(PointLookup):
    """Point lookups arriving at ``rate`` per second (Poisson), whatever the response times."""

    rate: int


class Workloads:
    """The default workload set, in run order."""

    @staticmethod
    def _reads() -> list[Workload]:
        reads: list[Workload] = []
        for c in (1, 16, 64):
            reads.append(
                PointLookup(
                    name=f"point-c{c}",
                    title=f"Primary-key lookup, 1 row, {c} concurrent tasks",
                    why=(
                        "The most common API query; c=1 is pure per-query latency, c=16 "
                        "matches the pool, c=64 oversubscribes it (tasks queue for connections)."
                    ),
                    category="read",
                    concurrency=c,
                    typed=False,
                ),
            )
        reads.append(
            PointLookup(
                name="point-c16-typed",
                title="Primary-key lookup into a msgspec Struct, 16 tasks",
                why="As point-c16, but every driver returns the same Struct (decode included).",
                category="read",
                concurrency=16,
                typed=True,
            ),
        )
        shapes: tuple[tuple[str, Literal["people", "wide"], int, int, str, str], ...] = (
            (
                "page-20",
                "people",
                20,
                16,
                "Page of 20 rows (10 columns)",
                "A list endpoint: a small multi-row fetch.",
            ),
            (
                "fetch-1000",
                "people",
                1_000,
                4,
                "1,000 rows (10 columns)",
                "A larger fetch (export, report, batch job): decode throughput.",
            ),
            (
                "wide-50",
                "wide",
                50,
                16,
                "50 type-heavy rows (13 columns: uuid, timestamptz, numeric, jsonb, bytea...)",
                "Decode cost of the expensive types, which the people rows barely have.",
            ),
        )
        for prefix, table, rows, c, title, why in shapes:
            for typed in (False, True):
                suffix = "-typed" if typed else ""
                into = " into msgspec Structs" if typed else ""
                reads.append(
                    RangeFetch(
                        name=f"{prefix}-c{c}{suffix}",
                        title=f"{title}{into}, {c} concurrent tasks",
                        why=why,
                        category="read",
                        concurrency=c,
                        rows_per_op=rows,
                        typed=typed,
                        table=table,
                    ),
                )
        return reads

    @staticmethod
    def _writes() -> list[Workload]:
        return [
            InsertOne(
                name="insert-c16",
                title="Single-row INSERT (autocommit), 16 concurrent tasks",
                why="The common write: one row per request.",
                category="write",
                concurrency=16,
            ),
            UpdateOne(
                name="update-c16",
                title="Single-row UPDATE by primary key (autocommit), 16 concurrent tasks",
                why="The other common write; random rows of 10,000 (occasional lock waits).",
                category="write",
                concurrency=16,
            ),
            Transfer(
                name="transaction-c16",
                title="Transaction: SELECT ... FOR UPDATE, 2 UPDATEs, 1 INSERT; 16 tasks",
                why="A short business transaction: checkout, BEGIN/COMMIT and 4 round trips.",
                category="write",
                concurrency=16,
                exclusive=True,
            ),
            BulkInsert(
                name="executemany-1000",
                title="executemany: 1,000-row INSERT batch, 1 task",
                why="Bulk insert through each driver's executemany (all-or-nothing).",
                category="bulk",
                concurrency=1,
                rows_per_op=1_000,
                exclusive=True,
                max_ops=200,
                method="executemany",
            ),
            BulkInsert(
                name="copy-5000",
                title="Binary COPY FROM STDIN: 5,000 rows, 1 task",
                why="The fastest way to load rows, for every driver that supports it.",
                category="bulk",
                concurrency=1,
                rows_per_op=5_000,
                exclusive=True,
                max_ops=200,
                method="copy",
            ),
        ]

    @staticmethod
    def _open_loop(rates: Sequence[int]) -> list[Workload]:
        return [
            OpenLoop(
                name=f"open-loop-{rate}",
                title=f"Open loop: point lookups arriving at {rate:,}/s (Poisson)",
                why=(
                    "Requests arrive at a fixed rate regardless of response time, so drivers "
                    "are compared at equal load; latency counts from the scheduled arrival."
                ),
                category="open-loop",
                concurrency=0,
                typed=False,
                rate=rate,
            )
            for rate in sorted(rates)
        ]

    @classmethod
    def all(cls, rates: Sequence[int]) -> list[Workload]:
        return [*cls._reads(), *cls._writes(), *cls._open_loop(rates)]
