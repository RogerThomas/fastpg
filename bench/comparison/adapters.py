"""One adapter per driver configuration, each going through the driver's own pool.

Every adapter runs the same SQL (``$n`` placeholders, or ``%s`` for psycopg) and is set up
the way that driver's documentation recommends for a service:

* **fastpg**: ``fastpg.create_pool`` (binary protocol, cached prepared statements). Reads and
  single writes are shared-mode statements; transactions and bulk writes check a connection
  out. ``fastpg`` keeps the default ``max_inflight_per_conn=8`` (it may pipeline shared
  statements on a busy connection); ``fastpg-inflight1`` sets it to 1, so like the other
  pools it never has more than one statement in flight per connection.
* **asyncpg**: ``asyncpg.create_pool`` (binary protocol, statement cache). jsonb is decoded
  with ``msgspec.json.decode`` via ``set_type_codec`` (asyncpg returns it as ``str``
  otherwise). ``asyncpg`` keeps the pool's default reset on release (one extra
  ``SELECT pg_advisory_unlock_all(); CLOSE ALL; UNLISTEN *; RESET ALL`` round trip per
  checkout); ``asyncpg-noreset`` passes a no-op ``reset=``, asyncpg's documented hook,
  which is safe here because no workload changes session state.
* **psycopg**: ``psycopg_pool.AsyncConnectionPool`` with ``autocommit=True`` (otherwise every
  checkout pays BEGIN/COMMIT round trips), server-side prepared statements after
  ``prepare_threshold`` (5) executions — the warm-up runs every statement well past that on
  every connection — and jsonb decoded with ``msgspec.json.decode`` (``set_json_loads``).
  Results use psycopg's default text format: ``binary=True`` measured equal or slower in
  this suite (``PsycopgAdapter(binary=True)`` to try it), e.g. 5,589 vs 5,939 ops/s on
  ``wide-50-c16`` and 578 vs 682 on ``fetch-1000-c4-typed``.
* **psqlpy**: ``psqlpy.ConnectionPool`` (binary protocol, statements prepared by default).
  It decodes jsonb natively; ``uuid`` columns come back as ``str`` unless a custom decoder
  is given (the typed variants give one).

Row types: the plain methods return each driver's default row (fastpg ``tuple``, asyncpg
``Record``, psycopg ``tuple``, psqlpy ``dict``); the ``*_typed`` ones return the same
msgspec Structs from every driver, built the fastest documented way for that driver.
"""

import uuid
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any, ClassVar, LiteralString, Protocol

import asyncpg
import msgspec
import psqlpy
from psycopg.rows import args_row
from psycopg.types.json import set_json_loads
from psycopg_pool import AsyncConnectionPool

import fastpg

from .data import EventRow, MismatchError, Person, Placeholders, Schema, WideRow

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from asyncpg import Record
    from psycopg import AsyncConnection
    from psycopg.rows import BaseRowFactory, TupleRow


class SQL:
    """The statements every driver runs (``$n`` placeholders)."""

    PERSON: ClassVar[LiteralString] = (
        "SELECT id, name, age, email, score, balance, active, external_id, created_at, birthday"
        " FROM fastpg_bench.people WHERE id = $1"
    )
    PEOPLE: ClassVar[LiteralString] = (
        "SELECT id, name, age, email, score, balance, active, external_id, created_at, birthday"
        " FROM fastpg_bench.people WHERE id BETWEEN $1 AND $2 ORDER BY id"
    )
    WIDE: ClassVar[LiteralString] = (
        "SELECT id, uid, created_at, day, amount, ratio, flag, title, body, note, payload, blob,"
        " big FROM fastpg_bench.wide WHERE id BETWEEN $1 AND $2 ORDER BY id"
    )
    INSERT_EVENT: ClassVar[LiteralString] = (
        "INSERT INTO fastpg_bench.events (account_id, kind, amount, created_at, note)"
        " VALUES ($1, $2, $3, $4, $5)"
    )
    ADD_BALANCE: ClassVar[LiteralString] = (
        "UPDATE fastpg_bench.accounts SET balance = balance + $1, updated_at = now() WHERE id = $2"
    )
    LOCK_PAIR: ClassVar[LiteralString] = (
        "SELECT id, balance FROM fastpg_bench.accounts WHERE id IN ($1, $2) ORDER BY id FOR UPDATE"
    )
    DEBIT: ClassVar[LiteralString] = (
        "UPDATE fastpg_bench.accounts SET balance = balance - $1 WHERE id = $2"
    )
    CREDIT: ClassVar[LiteralString] = (
        "UPDATE fastpg_bench.accounts SET balance = balance + $1 WHERE id = $2"
    )
    INSERT_TRANSFER: ClassVar[LiteralString] = (
        "INSERT INTO fastpg_bench.transfers (src, dst, amount) VALUES ($1, $2, $3)"
    )
    INSERT_BATCH: ClassVar[LiteralString] = (
        "INSERT INTO fastpg_bench.batch (account_id, kind, amount, created_at, note)"
        " VALUES ($1, $2, $3, $4, $5)"
    )


class PercentSQL:
    """``SQL`` with ``%s`` placeholders, for psycopg (:meth:`verify` proves they match)."""

    PERSON: ClassVar[LiteralString] = (
        "SELECT id, name, age, email, score, balance, active, external_id, created_at, birthday"
        " FROM fastpg_bench.people WHERE id = %s"
    )
    PEOPLE: ClassVar[LiteralString] = (
        "SELECT id, name, age, email, score, balance, active, external_id, created_at, birthday"
        " FROM fastpg_bench.people WHERE id BETWEEN %s AND %s ORDER BY id"
    )
    WIDE: ClassVar[LiteralString] = (
        "SELECT id, uid, created_at, day, amount, ratio, flag, title, body, note, payload, blob,"
        " big FROM fastpg_bench.wide WHERE id BETWEEN %s AND %s ORDER BY id"
    )
    INSERT_EVENT: ClassVar[LiteralString] = (
        "INSERT INTO fastpg_bench.events (account_id, kind, amount, created_at, note)"
        " VALUES (%s, %s, %s, %s, %s)"
    )
    ADD_BALANCE: ClassVar[LiteralString] = (
        "UPDATE fastpg_bench.accounts SET balance = balance + %s, updated_at = now() WHERE id = %s"
    )
    LOCK_PAIR: ClassVar[LiteralString] = (
        "SELECT id, balance FROM fastpg_bench.accounts WHERE id IN (%s, %s) ORDER BY id FOR UPDATE"
    )
    DEBIT: ClassVar[LiteralString] = (
        "UPDATE fastpg_bench.accounts SET balance = balance - %s WHERE id = %s"
    )
    CREDIT: ClassVar[LiteralString] = (
        "UPDATE fastpg_bench.accounts SET balance = balance + %s WHERE id = %s"
    )
    INSERT_TRANSFER: ClassVar[LiteralString] = (
        "INSERT INTO fastpg_bench.transfers (src, dst, amount) VALUES (%s, %s, %s)"
    )
    INSERT_BATCH: ClassVar[LiteralString] = (
        "INSERT INTO fastpg_bench.batch (account_id, kind, amount, created_at, note)"
        " VALUES (%s, %s, %s, %s, %s)"
    )
    COPY_BATCH: ClassVar[LiteralString] = (
        "COPY fastpg_bench.batch (account_id, kind, amount, created_at, note)"
        " FROM STDIN (FORMAT BINARY)"
    )
    _PAIRED: ClassVar[tuple[str, ...]] = (
        *("PERSON", "PEOPLE", "WIDE", "INSERT_EVENT", "ADD_BALANCE", "LOCK_PAIR"),
        *("DEBIT", "CREDIT", "INSERT_TRANSFER", "INSERT_BATCH"),
    )

    @classmethod
    def verify(cls) -> None:
        """Raise unless every statement here is exactly its ``SQL`` twin, placeholders aside."""
        for name in cls._PAIRED:
            if Placeholders.percent(getattr(SQL, name)) != getattr(cls, name):
                msg = f"PercentSQL.{name} differs from SQL.{name}"
                raise MismatchError(msg)


class Adapter(Protocol):
    """What the workloads call. One instance owns one pool, opened by :meth:`open`."""

    async def open(self, dsn: str, size: int) -> None:
        """Open the driver's pool with ``size`` connections (``min = max`` where supported)."""
        ...

    async def close(self) -> None:
        """Close the pool."""
        ...

    async def person(self, person_id: int) -> object:
        """One ``people`` row by primary key, as the driver's default row type."""
        ...

    async def person_typed(self, person_id: int) -> Person:
        """The same row as a ``Person`` Struct."""
        ...

    async def people(self, first: int, last: int) -> Sequence[object]:
        """``people`` rows ``first..last`` in id order, as the driver's default row type."""
        ...

    async def people_typed(self, first: int, last: int) -> list[Person]:
        """The same rows as ``Person`` Structs."""
        ...

    async def wide(self, first: int, last: int) -> Sequence[object]:
        """``wide`` rows ``first..last`` in id order, as the driver's default row type."""
        ...

    async def wide_typed(self, first: int, last: int) -> list[WideRow]:
        """The same rows as ``WideRow`` Structs."""
        ...

    async def insert_event(self, row: EventRow) -> None:
        """INSERT one ``events`` row (autocommit)."""
        ...

    async def add_balance(self, delta: int, account_id: int) -> None:
        """UPDATE one ``accounts`` row by primary key (autocommit)."""
        ...

    async def transfer(self, src: int, dst: int, amount: int) -> None:
        """One transaction: lock both accounts, debit, credit, record the transfer."""
        ...

    async def insert_batch(self, rows: list[EventRow]) -> None:
        """Insert ``rows`` with the driver's executemany, all-or-nothing."""
        ...

    async def copy_batch(self, rows: list[EventRow]) -> None:
        """Load ``rows`` with the driver's binary ``COPY FROM STDIN``."""
        ...


def _check_pair(rows: Sequence[object]) -> None:
    if len(rows) != 2:
        msg = f"transfer: locked {len(rows)} accounts, expected 2"
        raise MismatchError(msg)


# --------------------------------------------------------------------------------- fastpg


@dataclass(slots=True)
class FastpgAdapter:
    max_inflight_per_conn: int = 8
    _pool: fastpg.Pool = field(init=False)

    async def open(self, dsn: str, size: int) -> None:
        self._pool = await fastpg.create_pool(
            dsn,
            min_size=size,
            max_size=size,
            max_inflight_per_conn=self.max_inflight_per_conn,
        )

    async def close(self) -> None:
        await self._pool.close()

    async def person(self, person_id: int) -> tuple[object, ...]:
        return await self._pool.select_one(SQL.PERSON, person_id)

    async def person_typed(self, person_id: int) -> Person:
        return await self._pool.select_one(SQL.PERSON, person_id, into=Person)

    async def people(self, first: int, last: int) -> list[tuple[object, ...]]:
        return await self._pool.select_many(SQL.PEOPLE, first, last)

    async def people_typed(self, first: int, last: int) -> list[Person]:
        return await self._pool.select_many(SQL.PEOPLE, first, last, into=Person)

    async def wide(self, first: int, last: int) -> list[tuple[object, ...]]:
        return await self._pool.select_many(SQL.WIDE, first, last)

    async def wide_typed(self, first: int, last: int) -> list[WideRow]:
        return await self._pool.select_many(SQL.WIDE, first, last, into=WideRow)

    async def insert_event(self, row: EventRow) -> None:
        await self._pool.execute(SQL.INSERT_EVENT, *row)

    async def add_balance(self, delta: int, account_id: int) -> None:
        await self._pool.execute(SQL.ADD_BALANCE, delta, account_id)

    async def transfer(self, src: int, dst: int, amount: int) -> None:
        async with self._pool.transaction() as tx:
            _check_pair(await tx.select_many(SQL.LOCK_PAIR, src, dst))
            await tx.execute(SQL.DEBIT, amount, src)
            await tx.execute(SQL.CREDIT, amount, dst)
            await tx.execute(SQL.INSERT_TRANSFER, src, dst, amount)

    async def insert_batch(self, rows: list[EventRow]) -> None:
        await self._pool.execute_many(SQL.INSERT_BATCH, rows)

    async def copy_batch(self, rows: list[EventRow]) -> None:
        await self._pool.copy_in(
            (Schema.NAME, Schema.BATCH_TABLE),
            rows,
            columns=Schema.BATCH_COLUMNS,
        )


# -------------------------------------------------------------------------------- asyncpg


def _json_text(value: object) -> str:
    return msgspec.json.encode(value).decode()


async def _asyncpg_init(conn: asyncpg.Connection[Record]) -> None:
    await conn.set_type_codec(
        "jsonb",
        encoder=_json_text,
        decoder=msgspec.json.decode,
        schema="pg_catalog",
    )


async def _keep_session(conn: asyncpg.Connection[Record]) -> None:
    """No-op ``reset=``: skip the reset query (no workload changes session state)."""
    del conn


@dataclass(slots=True)
class AsyncpgAdapter:
    reset: bool = True
    _pool: asyncpg.Pool[Record] = field(init=False)

    async def open(self, dsn: str, size: int) -> None:
        self._pool = await asyncpg.create_pool(
            dsn,
            min_size=size,
            max_size=size,
            init=_asyncpg_init,
            reset=None if self.reset else _keep_session,
        )

    async def close(self) -> None:
        await self._pool.close()

    async def person(self, person_id: int) -> Record | None:
        return await self._pool.fetchrow(SQL.PERSON, person_id)

    async def person_typed(self, person_id: int) -> Person:
        record = await self._pool.fetchrow(SQL.PERSON, person_id)
        if record is None:
            msg = f"person {person_id}: no row"
            raise MismatchError(msg)
        return Person(*record)

    async def people(self, first: int, last: int) -> list[Record]:
        return await self._pool.fetch(SQL.PEOPLE, first, last)

    async def people_typed(self, first: int, last: int) -> list[Person]:
        return [Person(*r) for r in await self._pool.fetch(SQL.PEOPLE, first, last)]

    async def wide(self, first: int, last: int) -> list[Record]:
        return await self._pool.fetch(SQL.WIDE, first, last)

    async def wide_typed(self, first: int, last: int) -> list[WideRow]:
        return [WideRow(*r) for r in await self._pool.fetch(SQL.WIDE, first, last)]

    async def insert_event(self, row: EventRow) -> None:
        await self._pool.execute(SQL.INSERT_EVENT, *row)

    async def add_balance(self, delta: int, account_id: int) -> None:
        await self._pool.execute(SQL.ADD_BALANCE, delta, account_id)

    async def transfer(self, src: int, dst: int, amount: int) -> None:
        async with self._pool.acquire() as conn, conn.transaction():
            _check_pair(await conn.fetch(SQL.LOCK_PAIR, src, dst))
            await conn.execute(SQL.DEBIT, amount, src)
            await conn.execute(SQL.CREDIT, amount, dst)
            await conn.execute(SQL.INSERT_TRANSFER, src, dst, amount)

    async def insert_batch(self, rows: list[EventRow]) -> None:
        await self._pool.executemany(SQL.INSERT_BATCH, rows)

    async def copy_batch(self, rows: list[EventRow]) -> None:
        await self._pool.copy_records_to_table(
            Schema.BATCH_TABLE,
            records=rows,
            columns=Schema.BATCH_COLUMNS,
            schema_name=Schema.NAME,
        )


# -------------------------------------------------------------------------------- psycopg

type _PsycopgPool = AsyncConnectionPool[AsyncConnection[TupleRow]]


async def _psycopg_configure(conn: AsyncConnection[TupleRow]) -> None:
    set_json_loads(msgspec.json.decode, conn)


@dataclass(slots=True)
class PsycopgAdapter:
    binary: bool = False
    _pool: _PsycopgPool = field(init=False)
    _person_row: BaseRowFactory[Person] = field(
        init=False,
        default_factory=partial(args_row, Person),
    )
    _wide_row: BaseRowFactory[WideRow] = field(
        init=False,
        default_factory=partial(args_row, WideRow),
    )

    async def open(self, dsn: str, size: int) -> None:
        PercentSQL.verify()
        pool: _PsycopgPool = AsyncConnectionPool(
            dsn,
            min_size=size,
            max_size=size,
            open=False,
            kwargs={"autocommit": True},
            configure=_psycopg_configure,
        )
        await pool.open(wait=True)
        self._pool = pool

    async def close(self) -> None:
        await self._pool.close()

    async def person(self, person_id: int) -> TupleRow | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(PercentSQL.PERSON, (person_id,), binary=self.binary)
            return await cur.fetchone()

    async def person_typed(self, person_id: int) -> Person:
        async with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=self._person_row, binary=self.binary)
            await cur.execute(PercentSQL.PERSON, (person_id,))
            row = await cur.fetchone()
        if row is None:
            msg = f"person {person_id}: no row"
            raise MismatchError(msg)
        return row

    async def people(self, first: int, last: int) -> list[TupleRow]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(PercentSQL.PEOPLE, (first, last), binary=self.binary)
            return await cur.fetchall()

    async def people_typed(self, first: int, last: int) -> list[Person]:
        async with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=self._person_row, binary=self.binary)
            await cur.execute(PercentSQL.PEOPLE, (first, last))
            return await cur.fetchall()

    async def wide(self, first: int, last: int) -> list[TupleRow]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(PercentSQL.WIDE, (first, last), binary=self.binary)
            return await cur.fetchall()

    async def wide_typed(self, first: int, last: int) -> list[WideRow]:
        async with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=self._wide_row, binary=self.binary)
            await cur.execute(PercentSQL.WIDE, (first, last))
            return await cur.fetchall()

    async def insert_event(self, row: EventRow) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(PercentSQL.INSERT_EVENT, row)

    async def add_balance(self, delta: int, account_id: int) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(PercentSQL.ADD_BALANCE, (delta, account_id))

    async def transfer(self, src: int, dst: int, amount: int) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(PercentSQL.LOCK_PAIR, (src, dst))
            _check_pair(await cur.fetchall())
            await conn.execute(PercentSQL.DEBIT, (amount, src))
            await conn.execute(PercentSQL.CREDIT, (amount, dst))
            await conn.execute(PercentSQL.INSERT_TRANSFER, (src, dst, amount))

    async def insert_batch(self, rows: list[EventRow]) -> None:
        # psycopg's executemany pipelines the statements; the transaction makes it
        # all-or-nothing, like asyncpg's and fastpg's (in autocommit each row would commit).
        async with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.executemany(PercentSQL.INSERT_BATCH, rows)

    async def copy_batch(self, rows: list[EventRow]) -> None:
        async with (
            self._pool.connection() as conn,
            conn.cursor() as cur,
            cur.copy(PercentSQL.COPY_BATCH) as copy,
        ):
            copy.set_types(list(Schema.BATCH_TYPES))
            for row in rows:
                await copy.write_row(row)


# --------------------------------------------------------------------------------- psqlpy


def _uuid_from_bytes(raw: bytes) -> uuid.UUID:
    return uuid.UUID(bytes=raw)


@dataclass(slots=True)
class PsqlpyAdapter:
    _pool: psqlpy.ConnectionPool = field(init=False)
    # psqlpy returns uuid columns as str unless told how to decode them.
    _person_decoders: ClassVar[dict[str, Callable[[bytes], Any]]] = {
        "external_id": _uuid_from_bytes,
    }
    _wide_decoders: ClassVar[dict[str, Callable[[bytes], Any]]] = {"uid": _uuid_from_bytes}

    async def open(self, dsn: str, size: int) -> None:
        self._pool = psqlpy.ConnectionPool(dsn=dsn, max_db_pool_size=size)

    async def close(self) -> None:
        self._pool.close()

    async def person(self, person_id: int) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            result = await conn.fetch_row(SQL.PERSON, [person_id])
        return result.result()

    async def person_typed(self, person_id: int) -> Person:
        async with self._pool.acquire() as conn:
            result = await conn.fetch_row(SQL.PERSON, [person_id])
        return Person(*result.result(as_tuple=True, custom_decoders=self._person_decoders))

    async def people(self, first: int, last: int) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            result = await conn.fetch(SQL.PEOPLE, [first, last])
        return result.result()

    async def people_typed(self, first: int, last: int) -> list[Person]:
        async with self._pool.acquire() as conn:
            result = await conn.fetch(SQL.PEOPLE, [first, last])
        rows = result.result(as_tuple=True, custom_decoders=self._person_decoders)
        return [Person(*r) for r in rows]

    async def wide(self, first: int, last: int) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            result = await conn.fetch(SQL.WIDE, [first, last])
        return result.result()

    async def wide_typed(self, first: int, last: int) -> list[WideRow]:
        async with self._pool.acquire() as conn:
            result = await conn.fetch(SQL.WIDE, [first, last])
        rows = result.result(as_tuple=True, custom_decoders=self._wide_decoders)
        return [WideRow(*r) for r in rows]

    async def insert_event(self, row: EventRow) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(SQL.INSERT_EVENT, list(row))

    async def add_balance(self, delta: int, account_id: int) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(SQL.ADD_BALANCE, [delta, account_id])

    async def transfer(self, src: int, dst: int, amount: int) -> None:
        async with self._pool.acquire() as conn, conn.transaction() as tx:
            result = await tx.fetch(SQL.LOCK_PAIR, [src, dst])
            _check_pair(result.result())
            await tx.execute(SQL.DEBIT, [amount, src])
            await tx.execute(SQL.CREDIT, [amount, dst])
            await tx.execute(SQL.INSERT_TRANSFER, [src, dst, amount])

    async def insert_batch(self, rows: list[EventRow]) -> None:
        async with self._pool.acquire() as conn, conn.transaction() as tx:
            await tx.execute_many(SQL.INSERT_BATCH, rows)

    async def copy_batch(self, rows: list[EventRow]) -> None:
        async with self._pool.acquire() as conn:
            await conn.copy_records_to_table(
                Schema.BATCH_TABLE,
                rows,
                columns=Schema.BATCH_COLUMNS,
                schema_name=Schema.NAME,
            )


# ------------------------------------------------------------------------------ registry


@dataclass(frozen=True, slots=True, kw_only=True)
class Config:
    """A named driver configuration: one column of the results."""

    name: str
    package: str  # distribution name, for version reporting
    factory: Callable[[], Adapter]
    notes: str
    # Set when this configuration only differs from another one in shared-mode statements:
    # workloads that check a connection out (transactions, bulk writes) would run the very
    # same code path, so they're skipped for it.
    shared_variant_of: str | None = None


class Configs:
    """Every driver configuration, in report order."""

    @staticmethod
    def _fastpg_inflight1() -> FastpgAdapter:
        return FastpgAdapter(max_inflight_per_conn=1)

    @staticmethod
    def _asyncpg_noreset() -> AsyncpgAdapter:
        return AsyncpgAdapter(reset=False)

    @classmethod
    def all(cls) -> tuple[Config, ...]:
        return (
            Config(
                name="fastpg",
                package="fastpg",
                factory=FastpgAdapter,
                notes="create_pool defaults: max_inflight_per_conn=8 (may pipeline)",
            ),
            Config(
                name="fastpg-inflight1",
                package="fastpg",
                factory=cls._fastpg_inflight1,
                notes="max_inflight_per_conn=1: one statement per connection, no pipelining",
                shared_variant_of="fastpg",
            ),
            Config(
                name="asyncpg",
                package="asyncpg",
                factory=AsyncpgAdapter,
                notes="create_pool defaults (reset query on every release)",
            ),
            Config(
                name="asyncpg-noreset",
                package="asyncpg",
                factory=cls._asyncpg_noreset,
                notes="create_pool(reset=<no-op>): skips the reset round trip",
            ),
            Config(
                name="psycopg",
                package="psycopg",
                factory=PsycopgAdapter,
                notes="AsyncConnectionPool, autocommit, text results (its fastest here)",
            ),
            Config(
                name="psqlpy",
                package="psqlpy",
                factory=PsqlpyAdapter,
                notes="ConnectionPool defaults (prepared statements)",
            ),
        )
