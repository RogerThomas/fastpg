"""The suite's own schema and data, the expected rows, and the correctness checks.

Everything lives in the ``fastpg_bench`` schema, which the suite drops and recreates at the
start of a run (``--keep-schema`` reuses it), so it needs nothing pre-existing and leaves
every other table alone. Seed data is generated server side from closed-form formulas
(``generate_series``, no ``random()``); the expected rows are computed in Python from the
same formulas, so a check compares each driver against an independent ground truth, not
against another driver.

Setup, resets and the write checks run through psycopg (libpq), not through fastpg.
"""

import datetime as dt
import decimal
import hashlib
import re
import uuid
from typing import TYPE_CHECKING, ClassVar, LiteralString, TypeIs

import msgspec
from asyncpg import Record
from drivers.base import TABLE_ROWS, Person, expected_person
from psycopg.errors import InsufficientPrivilege

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from psycopg import AsyncConnection


class WideRow(msgspec.Struct):
    """One ``wide`` row: the type-heavy shape (uuid, timestamptz, numeric, jsonb, bytea...)."""

    id: int
    uid: uuid.UUID
    created_at: dt.datetime
    day: dt.date
    amount: decimal.Decimal
    ratio: float
    flag: bool
    title: str
    body: str
    note: str | None
    payload: dict[str, object]
    blob: bytes
    big: int


type EventRow = tuple[int, str, decimal.Decimal, dt.datetime, str | None]


class MismatchError(AssertionError):
    """A driver returned or wrote different data than expected: its timings are void."""


class Expected:
    """Closed-form row formulas shared by the seed SQL and the Python-side checks."""

    people_rows: ClassVar[int] = TABLE_ROWS
    wide_rows: ClassVar[int] = 10_000
    accounts: ClassVar[int] = 10_000
    initial_balance: ClassVar[int] = 1_000_000
    _epoch: ClassVar[dt.datetime] = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
    _day0: ClassVar[dt.date] = dt.date(2000, 1, 1)
    _fill: ClassVar[str] = "lorem ipsum "

    @classmethod
    def _rpad(cls, text: str, width: int) -> str:
        """PostgreSQL's ``rpad(text, width, fill)``."""
        if len(text) >= width:
            return text[:width]
        repeats = (width - len(text)) // len(cls._fill) + 1
        return (text + cls._fill * repeats)[:width]

    @staticmethod
    def _md5(text: str) -> bytes:
        return hashlib.md5(text.encode(), usedforsecurity=False).digest()

    @staticmethod
    def person(i: int) -> Person:
        return expected_person(i)

    @classmethod
    def wide(cls, i: int) -> WideRow:
        return WideRow(
            id=i,
            uid=uuid.UUID(bytes=cls._md5(f"{i}-wide")),
            created_at=cls._epoch + dt.timedelta(seconds=i * 37),
            day=cls._day0 + dt.timedelta(days=i % 9000),
            amount=decimal.Decimal((i * 7919) % 100_000_000).scaleb(-4),
            ratio=i / 8,
            flag=i % 3 == 0,
            title=f"title-{i}",
            body=cls._rpad(f"body-{i} ", 160 + i % 64),
            note=None if i % 5 == 0 else f"note-{i}",
            payload={
                "n": i,
                "ref": cls._md5(str(i)).hex(),
                "ok": i % 2 == 0,
                "tags": [f"t{i % 10}", f"t{i % 7}"],
            },
            blob=cls._md5(str(i)) + cls._md5(str(i + 1)),
            big=i * 1_000_003,
        )

    @classmethod
    def event(cls, key: int) -> EventRow:
        """The row written for ``key`` by the insert workloads (checked by ``Schema.EVENTS_OK``)."""
        return (
            key,
            f"kind-{key % 5}",
            decimal.Decimal(key % 100_000).scaleb(-2),
            cls._epoch + dt.timedelta(seconds=key),
            None if key % 3 == 0 else f"note-{key}",
        )

    @staticmethod
    def transfer_amount(src: int, dst: int) -> int:
        return 1 + (src + dst) % 50


class Schema:
    """DDL, seed data, resets and the server-side write checks, all in ``fastpg_bench``."""

    NAME: ClassVar[str] = "fastpg_bench"
    BATCH_TABLE: ClassVar[str] = "batch"
    BATCH_COLUMNS: ClassVar[tuple[str, ...]] = (
        "account_id",
        "kind",
        "amount",
        "created_at",
        "note",
    )
    BATCH_TYPES: ClassVar[tuple[str, ...]] = ("int4", "text", "numeric", "timestamptz", "text")

    DROP: ClassVar[LiteralString] = "DROP SCHEMA IF EXISTS fastpg_bench CASCADE"
    CREATE: ClassVar[LiteralString] = """
CREATE SCHEMA fastpg_bench;
CREATE TABLE fastpg_bench.people (
    id          integer          PRIMARY KEY,
    name        text             NOT NULL,
    age         integer          NOT NULL,
    email       text,
    score       double precision NOT NULL,
    balance     numeric(12, 2)   NOT NULL,
    active      boolean          NOT NULL,
    external_id uuid             NOT NULL,
    created_at  timestamptz      NOT NULL,
    birthday    date
);
CREATE TABLE fastpg_bench.wide (
    id         integer          PRIMARY KEY,
    uid        uuid             NOT NULL,
    created_at timestamptz      NOT NULL,
    day        date             NOT NULL,
    amount     numeric(14, 4)   NOT NULL,
    ratio      double precision NOT NULL,
    flag       boolean          NOT NULL,
    title      text             NOT NULL,
    body       text             NOT NULL,
    note       text,
    payload    jsonb            NOT NULL,
    blob       bytea            NOT NULL,
    big        bigint           NOT NULL
);
CREATE TABLE fastpg_bench.accounts (
    id         integer     PRIMARY KEY,
    balance    bigint      NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE fastpg_bench.events (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id integer        NOT NULL,
    kind       text           NOT NULL,
    amount     numeric(12, 2) NOT NULL,
    created_at timestamptz    NOT NULL,
    note       text
);
CREATE TABLE fastpg_bench.transfers (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    src        integer     NOT NULL,
    dst        integer     NOT NULL,
    amount     bigint      NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
-- Bulk loads write hundreds of thousands of rows per run: unlogged keeps WAL volume (and
-- the runner's disk) out of a client measurement. Every driver writes to the same table.
CREATE UNLOGGED TABLE fastpg_bench.batch (
    account_id integer        NOT NULL,
    kind       text           NOT NULL,
    amount     numeric(12, 2) NOT NULL,
    created_at timestamptz    NOT NULL,
    note       text
);
"""
    # Same formulas as docker/initdb/02-seed.sql (people) and Expected (wide, accounts).
    SEED: ClassVar[LiteralString] = """
INSERT INTO fastpg_bench.people
SELECT
    i,
    'person-' || i,
    18 + (i % 60),
    CASE WHEN i % 7 = 0 THEN NULL ELSE 'person' || i || '@example.com' END,
    (i % 1000) / 10.0,
    ((i * 37) % 100000) / 100.0,
    i % 2 = 0,
    md5(i::text)::uuid,
    timestamptz '2024-01-01 00:00:00+00' + make_interval(secs => i * 61),
    CASE WHEN i % 11 = 0 THEN NULL ELSE date '1950-01-01' + (i % 20000) END
FROM generate_series(1, 100000) AS s(i);
INSERT INTO fastpg_bench.wide
SELECT
    i,
    md5(i || '-wide')::uuid,
    timestamptz '2024-01-01 00:00:00+00' + make_interval(secs => i * 37),
    date '2000-01-01' + (i % 9000),
    ((i::bigint * 7919) % 100000000) / 10000.0,
    i / 8.0,
    i % 3 = 0,
    'title-' || i,
    rpad('body-' || i || ' ', 160 + (i % 64), 'lorem ipsum '),
    CASE WHEN i % 5 = 0 THEN NULL ELSE 'note-' || i END,
    jsonb_build_object(
        'n', i,
        'ref', md5(i::text),
        'ok', i % 2 = 0,
        'tags', jsonb_build_array('t' || (i % 10), 't' || (i % 7))
    ),
    decode(md5(i::text), 'hex') || decode(md5((i + 1)::text), 'hex'),
    i::bigint * 1000003
FROM generate_series(1, 10000) AS s(i);
INSERT INTO fastpg_bench.accounts (id, balance)
SELECT i, 1000000 FROM generate_series(1, 10000) AS s(i);
"""
    VACUUM: ClassVar[tuple[LiteralString, ...]] = (
        "VACUUM (ANALYZE) fastpg_bench.people",
        "VACUUM (ANALYZE) fastpg_bench.wide",
        "VACUUM (ANALYZE) fastpg_bench.accounts",
    )
    RESET_WRITES: ClassVar[LiteralString] = (
        "TRUNCATE fastpg_bench.events, fastpg_bench.transfers, fastpg_bench.batch RESTART IDENTITY"
    )
    VACUUM_ACCOUNTS: ClassVar[LiteralString] = "VACUUM fastpg_bench.accounts"
    BALANCE_TOTAL: ClassVar[LiteralString] = (
        "SELECT sum(balance)::bigint FROM fastpg_bench.accounts"
    )
    # (rows, rows whose content doesn't match Expected.event(account_id))
    _EVENT_PREDICATE: ClassVar[LiteralString] = """
    count(*),
    count(*) FILTER (WHERE NOT (
        kind = 'kind-' || (account_id % 5)
        AND amount = (account_id % 100000) / 100.0
        AND created_at = timestamptz '2024-01-01 00:00:00+00' + make_interval(secs => account_id)
        AND note IS NOT DISTINCT FROM
            CASE WHEN account_id % 3 = 0 THEN NULL ELSE 'note-' || account_id END
    ))"""
    EVENTS_OK: ClassVar[LiteralString] = f"SELECT {_EVENT_PREDICATE} FROM fastpg_bench.events"  # noqa: S608
    BATCH_OK: ClassVar[LiteralString] = f"SELECT {_EVENT_PREDICATE} FROM fastpg_bench.batch"  # noqa: S608
    TRANSFERS_OK: ClassVar[LiteralString] = """
SELECT count(*), count(*) FILTER (WHERE NOT (src <> dst AND amount = 1 + (src + dst) % 50))
FROM fastpg_bench.transfers"""

    @classmethod
    async def create(cls, conn: AsyncConnection, *, keep: bool) -> None:
        """(Re)create and seed the schema; with ``keep``, reuse it if already seeded."""
        if keep:
            cur = await conn.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_schema = %s",
                (cls.NAME,),
            )
            row = await cur.fetchone()
            if row is not None and row[0] == 6:
                return
        await conn.execute(cls.DROP)
        await conn.execute(cls.CREATE)
        await conn.execute(cls.SEED)
        for statement in cls.VACUUM:
            await conn.execute(statement)

    @classmethod
    async def settle(cls, conn: AsyncConnection) -> bool:
        """Start a run from the same server state: no rows or dirty pages left by the last one.

        Empties the write tables (the bulk loads leave up to a million rows), then writes out
        every dirty buffer, so the next run never pays for evicting the previous run's pages.
        Returns False if the role may not ``CHECKPOINT`` (needs superuser or pg_checkpoint).
        """
        await conn.execute(cls.RESET_WRITES)
        try:
            await conn.execute("CHECKPOINT")
        except InsufficientPrivilege:
            return False
        return True


class Placeholders:
    """``$n`` SQL (fastpg, asyncpg, psqlpy) to ``%s`` SQL (psycopg)."""

    _DOLLAR: ClassVar[re.Pattern[str]] = re.compile(r"\$(\d+)")

    @classmethod
    def percent(cls, query: str) -> str:
        numbers = [int(n) for n in cls._DOLLAR.findall(query)]
        if numbers != list(range(1, len(numbers) + 1)):
            msg = f"parameters must appear once each, in order, to become %s: {query!r}"
            raise ValueError(msg)
        return cls._DOLLAR.sub("%s", query)


def _is_struct(row: object) -> TypeIs[msgspec.Struct]:
    return isinstance(row, msgspec.Struct)


def _is_mapping(row: object) -> TypeIs[dict[str, object]]:
    return isinstance(row, dict)


def _is_sequence(row: object) -> TypeIs[tuple[object, ...] | list[object] | Record]:
    return isinstance(row, tuple | list | Record)


def _values(row: object) -> tuple[object, ...]:
    """A native row's values in column order, whatever the driver's row type."""
    if _is_mapping(row):
        return tuple(row.values())
    if _is_struct(row):
        return msgspec.structs.astuple(row)
    if _is_sequence(row):
        return tuple(row)
    msg = f"unexpected row type {type(row).__name__}"
    raise MismatchError(msg)


def _native_value(want: object, got: object) -> object:
    """Map a driver's default Python type to the expected one, where they differ by design.

    psqlpy returns ``uuid`` columns as ``str`` by default: compared as a UUID here. Nothing
    else is converted; every other value must already compare equal.
    """
    if isinstance(want, uuid.UUID) and isinstance(got, str):
        return uuid.UUID(got)
    return got


def _check_typed(label: str, want: msgspec.Struct, got: object) -> None:
    if type(got) is not type(want) or not _is_struct(got):
        msg = f"{label}: expected a {type(want).__name__}, got {type(got).__name__}: {got!r}"
        raise MismatchError(msg)
    for name, w, g in zip(
        want.__struct_fields__,
        msgspec.structs.astuple(want),
        msgspec.structs.astuple(got),
        strict=True,
    ):
        # A subclass is fine (asyncpg returns its own uuid.UUID subclass); a str is not.
        if w != g or (w is not None and not isinstance(g, type(w))):
            msg = f"{label}: field {name!r} is {g!r} ({type(g).__name__}), expected {w!r}"
            raise MismatchError(msg)


def _check_native(label: str, want: msgspec.Struct, got: object) -> None:
    values = _values(got)
    expected = msgspec.structs.astuple(want)
    if len(values) != len(expected):
        msg = f"{label}: {len(values)} columns, expected {len(expected)}"
        raise MismatchError(msg)
    for name, w, g in zip(want.__struct_fields__, expected, values, strict=True):
        if _native_value(w, g) != w:
            msg = f"{label}: column {name!r} is {g!r} ({type(g).__name__}), expected {w!r}"
            raise MismatchError(msg)


def check_rows(
    label: str,
    want: Sequence[msgspec.Struct],
    got: Iterable[object],
    *,
    typed: bool,
) -> None:
    """Raise MismatchError unless ``got`` holds exactly ``want``'s rows, in order.

    ``typed``: each row must be the same Struct class with identically typed fields.
    Otherwise (native rows): the same values column by column, in the driver's own types.
    """
    rows = list(got)
    if len(rows) != len(want):
        msg = f"{label}: {len(rows)} rows, expected {len(want)}"
        raise MismatchError(msg)
    for i, (w, g) in enumerate(zip(want, rows, strict=True)):
        if typed:
            _check_typed(f"{label} row {i}", w, g)
        else:
            _check_native(f"{label} row {i}", w, g)
