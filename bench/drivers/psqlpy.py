"""psqlpy (Rust, tokio-postgres): binary protocol, ``$n`` placeholders, statements prepared and
cached per connection by default (``prepared=True``).

Default rows are ``dict`` via ``QueryResult.result()``. psqlpy decodes ``uuid`` columns to
``str``, so the struct variant uses its documented hooks: ``row_factory(class_row(Person))``
plus a ``custom_decoders`` entry turning the raw 16 uuid bytes into ``uuid.UUID``.
"""

import uuid
from typing import Any

import psqlpy
from psqlpy.row_factories import class_row

from .base import AsyncSpec, Person

SQL_ONE = (
    "SELECT id, name, age, email, score, balance, active, external_id, created_at, birthday "
    "FROM people WHERE id = $1"
)
SQL_RANGE = (
    "SELECT id, name, age, email, score, balance, active, external_id, created_at, birthday "
    "FROM people WHERE id BETWEEN $1 AND $2"
)


def _uuid_from_bytes(raw: bytes) -> uuid.UUID:
    return uuid.UUID(bytes=raw)


_DECODERS = {"external_id": _uuid_from_bytes}
_AS_PERSON = class_row(Person)


class _Pool:
    def __init__(self, pool: psqlpy.ConnectionPool) -> None:
        self._pool = pool

    async def close(self) -> None:
        self._pool.close()


class _DictPool(_Pool):
    async def fetch_one(self, person_id: int) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            row = await conn.fetch_row(SQL_ONE, [person_id])
        return row.result()


class _StructPool(_Pool):
    async def fetch_one(self, person_id: int) -> Person:
        async with self._pool.acquire() as conn:
            row = await conn.fetch_row(SQL_ONE, [person_id])
        return row.row_factory(_AS_PERSON, custom_decoders=_DECODERS)


class _Connection:
    _conn: psqlpy.Connection

    async def connect(self, dsn: str) -> None:
        self._conn = await psqlpy.connect(dsn=dsn)

    async def close(self) -> None:
        self._conn.close()

    async def _create_pool(self, dsn: str, size: int) -> psqlpy.ConnectionPool:
        return psqlpy.ConnectionPool(dsn=dsn, max_db_pool_size=size)


class PsqlpyDriver(_Connection):
    async def fetch_one(self, person_id: int) -> dict[str, Any]:
        row = await self._conn.fetch_row(SQL_ONE, [person_id])
        return row.result()

    async def fetch_range(self, start: int, n: int) -> list[dict[str, Any]]:
        result = await self._conn.fetch(SQL_RANGE, [start, start + n - 1])
        return result.result()

    async def create_pool(self, dsn: str, size: int) -> _DictPool:
        return _DictPool(await self._create_pool(dsn, size))


class PsqlpyStructDriver(_Connection):
    async def fetch_one(self, person_id: int) -> Person:
        row = await self._conn.fetch_row(SQL_ONE, [person_id])
        return row.row_factory(_AS_PERSON, custom_decoders=_DECODERS)

    async def fetch_range(self, start: int, n: int) -> list[Person]:
        result = await self._conn.fetch(SQL_RANGE, [start, start + n - 1])
        return result.row_factory(_AS_PERSON, custom_decoders=_DECODERS)

    async def create_pool(self, dsn: str, size: int) -> _StructPool:
        return _StructPool(await self._create_pool(dsn, size))


DRIVERS = (
    AsyncSpec(name="psqlpy", factory=PsqlpyDriver, package="psqlpy", rows="native"),
    AsyncSpec(
        name="psqlpy-struct",
        factory=PsqlpyStructDriver,
        package="psqlpy",
        rows="struct",
        notes="row_factory(class_row(Person)) + uuid custom_decoder",
    ),
)
