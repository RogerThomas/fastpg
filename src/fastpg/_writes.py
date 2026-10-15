"""Bulk writes: `execute_many`, `insert_many` and binary `copy_in`.

The native side (`native/src/bulk.rs`) does the work; this module picks the column names and
how to read them from each kind of row, and feeds rows to the native handle in chunks:

* each chunk is read and encoded to PostgreSQL binary format in Rust, on the loop thread
  (one chunk at a time, so other tasks run in between);
* at most `BulkSettings.in_flight` chunks are queued for the I/O task; a chunk is
  acknowledged once the server has answered for it (`execute_many`) or it has been written to
  the socket (COPY), so memory stays bounded for any input size.

Every call is atomic: on an error, or if the awaiting task is cancelled before the last
chunk was handed over, nothing is written, and a transaction opened by the caller is left
as it was (the rows run inside a savepoint). Errors carry `row_index`, the 0-based index of
the failing input row, when it is known.
"""

import asyncio
import sys
from asyncio import FIRST_COMPLETED
from collections import deque
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import IntEnum
from functools import cache, partial
from itertools import islice
from typing import TYPE_CHECKING, Any, ClassVar, TypeIs

import msgspec

from fastpg._native import RowReader

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastpg._native import Bulk, Connection

type Table = str | Sequence[str]
"""`"people"`, `"app.people"` (split on dots), or `("my.schema", "people")` for names
that contain dots. Each part is quoted, so it is matched exactly (case-sensitively)."""

type _Fields = tuple[tuple[str, str | None], ...]
"""`(attribute, column)` per field; column None when the field's alias can't name one."""

type _Starter = Callable[[asyncio.Future[None], asyncio.Future[int | None]], Bulk]
"""Starts the native operation, given its `ready` and `done` futures."""


class BulkSettings:
    """Chunking knobs (class attributes, so they can be tuned or patched in one place)."""

    execute_chunk: ClassVar[int] = 1024
    """Rows per chunk for `execute_many` / `insert_many`."""
    copy_chunk: ClassVar[int] = 4096
    """Rows per chunk for `copy_in`."""
    in_flight: ClassVar[int] = 2
    """Chunks queued for the I/O task at once: one being sent/executed, one behind it."""
    insert_group: ClassVar[int] = 64
    """Rows per `INSERT ... VALUES (..), (..)` statement in `insert_many` (a power of two,
    so it divides `execute_chunk`). A chunk's statements share one protocol `Sync`."""
    max_parameters: ClassVar[int] = 65_535
    """PostgreSQL's limit on parameters per statement."""


class RowKind(IntEnum):
    """How `RowReader` reads values (must match `ReaderKind` in `native/src/bulk.rs`)."""

    SEQUENCE = 0
    MAPPING = 1
    STRUCT = 2
    MODEL = 3


@dataclass(slots=True)
class RowPlan:
    """What to write for rows like the first one."""

    columns: list[str] | None
    """Column names; None means every column of the table, positionally."""
    reader: RowReader
    width: int
    """Values per row."""


def _is_mapping(obj: object) -> TypeIs[Mapping[object, object]]:
    return isinstance(obj, Mapping)


def _is_sequence(obj: object) -> TypeIs[Sequence[object]]:
    return isinstance(obj, Sequence) and not isinstance(obj, str | bytes | bytearray)


def _is_list(obj: Iterable[object]) -> TypeIs[list[object]]:
    return isinstance(obj, list)


@cache
def _struct_fields(cls: type[msgspec.Struct]) -> _Fields:
    """Columns are the *encoded* field names (`rename=`), as `select_*` matches them."""
    interned = {name: name for name in cls.__struct_fields__}
    return tuple((interned[f.name], f.encode_name) for f in msgspec.structs.fields(cls))


@cache
def _model_fields(cls: type[Any]) -> _Fields:
    """Columns are the aliases `select_*` matches pydantic fields by."""
    from fastpg._pydantic import ModelPlanner

    fields: dict[str, Any] = cls.model_fields
    return tuple((name, ModelPlanner.write_alias(name, info)) for name, info in fields.items())


def _pick(
    fields: _Fields,
    columns: Sequence[str] | None,
    cls: type[Any],
) -> tuple[list[str], list[str]]:
    """(attributes, columns) to write: every field, or the fields named by `columns`."""
    by_column = {c: a for a, c in fields if c is not None}
    if columns is None:
        unnamed = [a for a, c in fields if c is None]
        if unnamed:
            msg = (
                f"{cls.__qualname__} field(s) {unnamed} have AliasPath/AliasChoices aliases, "
                "which don't name a column; pass columns="
            )
            raise TypeError(msg)
        columns = list(by_column)
    unknown = [c for c in columns if c not in by_column]
    if unknown:
        msg = f"{cls.__qualname__} has no field for column(s) {unknown}"
        raise ValueError(msg)
    return [by_column[c] for c in columns], list(columns)


def _attribute_plan(
    kind: RowKind,
    fields: _Fields,
    columns: Sequence[str] | None,
    cls: type[Any],
) -> RowPlan:
    attrs, cols = _pick(fields, columns, cls)
    return RowPlan(cols, RowReader(kind, attrs, labels=cols, row_type=cls), len(cols))


def _mapping_plan(first: Mapping[object, object], columns: Sequence[str] | None) -> RowPlan:
    keys = list(first) if columns is None else list(columns)
    names = [k for k in keys if isinstance(k, str)]
    if len(names) != len(keys):
        msg = "mapping rows need str keys (column names)"
        raise TypeError(msg)
    reader = RowReader(RowKind.MAPPING, names, labels=names, exact=columns is None)
    return RowPlan(names, reader, len(names))


def _row_plan(first: object, columns: Sequence[str] | None) -> RowPlan:
    """Columns to write and how to read them, for rows like `first`."""
    if isinstance(columns, str):
        msg = "columns must be a sequence of column names, not a str"
        raise TypeError(msg)
    if isinstance(first, msgspec.Struct):
        cls = type(first)
        return _attribute_plan(RowKind.STRUCT, _struct_fields(cls), columns, cls)
    pydantic = sys.modules.get("pydantic")  # optional: only if the application imported it
    if pydantic is not None and isinstance(first, pydantic.BaseModel):
        cls = type(first)
        return _attribute_plan(RowKind.MODEL, _model_fields(cls), columns, cls)
    if _is_mapping(first):
        return _mapping_plan(first, columns)
    if _is_sequence(first):
        cols = None if columns is None else list(columns)
        reader = RowReader(RowKind.SEQUENCE, [], labels=cols or [])
        return RowPlan(cols, reader, len(first) if cols is None else len(cols))
    msg = (
        "rows must be msgspec Structs, pydantic models, mappings or sequences, "
        f"got {type(first).__qualname__}"
    )
    raise TypeError(msg)


def _chunks(rows: Iterable[object], size: int) -> Iterator[list[object]]:
    if _is_list(rows):
        for start in range(0, len(rows), size):
            yield rows[start : start + size]
        return
    it = iter(rows)
    while chunk := list(islice(it, size)):
        yield chunk


def _retrieve(future: asyncio.Future[Any]) -> None:
    """Mark a future's exception as retrieved (its awaiter is gone)."""
    if not future.cancelled():
        future.exception()


def _insert_prefix(target: str, plan: RowPlan) -> str:
    """`INSERT INTO "t" ("a", "b") VALUES ` (the row tuples are added natively)."""
    if plan.width == 0:
        msg = "rows have no columns to insert"
        raise ValueError(msg)
    column_list = ""
    if plan.columns is not None:
        column_list = f" ({', '.join(map(quote_ident, plan.columns))})"
    return f"INSERT INTO {target}{column_list} VALUES "  # noqa: S608 - identifiers are quoted


def _insert_group(width: int) -> int:
    """Rows per statement: `insert_group`, halved until the parameters fit."""
    group = BulkSettings.insert_group
    while group > 1 and group * width > BulkSettings.max_parameters:
        group //= 2
    return group


@dataclass(slots=True)
class _BulkRun:
    """Feeds chunks to one native bulk operation."""

    _loop: asyncio.AbstractEventLoop
    _bulk: Bulk
    _ready: asyncio.Future[None]
    _done: asyncio.Future[int | None]

    async def _feed(self, first: list[object], rest: Iterator[list[object]]) -> None:
        await self._ready
        pending: deque[asyncio.Future[None]] = deque()
        chunk: list[object] | None = first
        while chunk is not None and not self._done.done():  # done early = failed: stop
            ack: asyncio.Future[None] = self._loop.create_future()
            self._bulk.feed(ack, chunk)
            pending.append(ack)
            if len(pending) >= BulkSettings.in_flight:
                # Race the ack against `done`: if the operation fails (e.g. the connection
                # dies), queued acks may never resolve, and `done` explains why.
                await asyncio.wait((pending.popleft(), self._done), return_when=FIRST_COMPLETED)
            chunk = next(rest, None)
        self._bulk.finish()

    def _server_error(self, error: BaseException) -> BaseException | None:
        """An earlier row's server-side failure, which explains `error` better."""
        if isinstance(error, asyncio.CancelledError) or not self._done.done():
            return None
        if self._done.cancelled():
            return None
        return self._done.exception()

    async def run(self, first: list[object], rest: Iterator[list[object]]) -> int:
        try:
            await self._feed(first, rest)
        except BaseException as e:
            self._bulk.abort()  # rolls back; `done` resolves to None unless the server failed
            self._done.add_done_callback(_retrieve)
            server_error = self._server_error(e)
            if server_error is not None:
                raise server_error from e
            raise
        return await self._done or 0


async def _run(
    loop: asyncio.AbstractEventLoop,
    start: _Starter,
    first: list[object],
    rest: Iterator[list[object]],
) -> int:
    ready: asyncio.Future[None] = loop.create_future()
    done: asyncio.Future[int | None] = loop.create_future()
    return await _BulkRun(loop, start(ready, done), ready, done).run(first, rest)


def quote_ident(name: str) -> str:
    """Quote one SQL identifier (always quoted, so it is matched case-sensitively)."""
    if not name or "\x00" in name:
        msg = f"invalid SQL identifier {name!r}"
        raise ValueError(msg)
    return '"' + name.replace('"', '""') + '"'


def quote_table(table: Table) -> str:
    """`"app.people"` / `("app", "people")` → `"app"."people"`."""
    parts = table.split(".") if isinstance(table, str) else list(table)
    if not 1 <= len(parts) <= 3:  # [catalog.]schema.table at most
        msg = f"invalid table name {table!r}"
        raise ValueError(msg)
    return ".".join(quote_ident(part) for part in parts)


async def execute_many(
    native: Connection,
    loop: asyncio.AbstractEventLoop,
    query: str,
    rows: Iterable[Sequence[object]],
) -> int:
    chunks = _chunks(rows, BulkSettings.execute_chunk)
    first = next(chunks, None)
    if first is None:
        return 0
    return await _run(loop, partial(native.execute_many, sql=query), first, chunks)


async def insert_many(
    native: Connection,
    loop: asyncio.AbstractEventLoop,
    table: Table,
    rows: Iterable[object],
    columns: Sequence[str] | None,
) -> int:
    target = quote_table(table)
    chunks = _chunks(rows, BulkSettings.execute_chunk)
    first = next(chunks, None)
    if first is None:
        return 0
    plan = _row_plan(first[0], columns)
    start = partial(
        native.insert_many,
        prefix=_insert_prefix(target, plan),
        width=plan.width,
        group=_insert_group(plan.width),
        reader=plan.reader,
    )
    return await _run(loop, start, first, chunks)


async def copy_in(
    native: Connection,
    loop: asyncio.AbstractEventLoop,
    table: Table,
    rows: Iterable[object],
    columns: Sequence[str] | None,
) -> int:
    target = quote_table(table)
    chunks = _chunks(rows, BulkSettings.copy_chunk)
    first = next(chunks, None)
    if first is None:
        return 0
    plan = _row_plan(first[0], columns)
    start = partial(native.copy_in, table=target, reader=plan.reader, columns=plan.columns)
    return await _run(loop, start, first, chunks)
