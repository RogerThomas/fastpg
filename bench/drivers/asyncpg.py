"""asyncpg: binary protocol, ``$n`` placeholders, LRU cache of named prepared statements.

Default rows are ``asyncpg.Record``; the struct variant builds ``Person(*record)``.
"""

import asyncpg
from asyncpg import Record

from .base import AsyncSpec, Person, PersonModel

SQL_ONE = (
    "SELECT id, name, age, email, score, balance, active, external_id, created_at, birthday "
    "FROM people WHERE id = $1"
)
SQL_RANGE = (
    "SELECT id, name, age, email, score, balance, active, external_id, created_at, birthday "
    "FROM people WHERE id BETWEEN $1 AND $2"
)


class _Pool:
    def __init__(self, pool: asyncpg.Pool[Record]) -> None:
        self._pool = pool

    async def close(self) -> None:
        await self._pool.close()


class _RecordPool(_Pool):
    async def fetch_one(self, person_id: int) -> Record | None:
        return await self._pool.fetchrow(SQL_ONE, person_id)


class _StructPool(_Pool):
    async def fetch_one(self, person_id: int) -> Person | None:
        record = await self._pool.fetchrow(SQL_ONE, person_id)
        return None if record is None else Person(*record)


class _ModelPool(_Pool):
    async def fetch_one(self, person_id: int) -> PersonModel | None:
        record = await self._pool.fetchrow(SQL_ONE, person_id)
        return None if record is None else PersonModel.model_validate(dict(record))


class _ConstructPool(_Pool):
    async def fetch_one(self, person_id: int) -> PersonModel | None:
        record = await self._pool.fetchrow(SQL_ONE, person_id)
        return None if record is None else PersonModel.model_construct(**record)


class _Connection:
    _conn: asyncpg.Connection[Record]

    async def connect(self, dsn: str) -> None:
        self._conn = await asyncpg.connect(dsn)

    async def close(self) -> None:
        await self._conn.close()


class AsyncpgDriver(_Connection):
    async def fetch_one(self, person_id: int) -> Record | None:
        return await self._conn.fetchrow(SQL_ONE, person_id)

    async def fetch_range(self, start: int, n: int) -> list[Record]:
        return await self._conn.fetch(SQL_RANGE, start, start + n - 1)

    async def create_pool(self, dsn: str, size: int) -> _RecordPool:
        return _RecordPool(await asyncpg.create_pool(dsn, min_size=size, max_size=size))


class AsyncpgStructDriver(_Connection):
    async def fetch_one(self, person_id: int) -> Person | None:
        record = await self._conn.fetchrow(SQL_ONE, person_id)
        return None if record is None else Person(*record)

    async def fetch_range(self, start: int, n: int) -> list[Person]:
        records = await self._conn.fetch(SQL_RANGE, start, start + n - 1)
        return [Person(*r) for r in records]

    async def create_pool(self, dsn: str, size: int) -> _StructPool:
        return _StructPool(await asyncpg.create_pool(dsn, min_size=size, max_size=size))


class AsyncpgPydanticDriver(_Connection):
    async def fetch_one(self, person_id: int) -> PersonModel | None:
        record = await self._conn.fetchrow(SQL_ONE, person_id)
        return None if record is None else PersonModel.model_validate(dict(record))

    async def fetch_range(self, start: int, n: int) -> list[PersonModel]:
        records = await self._conn.fetch(SQL_RANGE, start, start + n - 1)
        return [PersonModel.model_validate(dict(r)) for r in records]

    async def create_pool(self, dsn: str, size: int) -> _ModelPool:
        return _ModelPool(await asyncpg.create_pool(dsn, min_size=size, max_size=size))


class AsyncpgConstructDriver(_Connection):
    """Unvalidated baseline, comparable to fastpg's direct pydantic construction."""

    async def fetch_one(self, person_id: int) -> PersonModel | None:
        record = await self._conn.fetchrow(SQL_ONE, person_id)
        return None if record is None else PersonModel.model_construct(**record)

    async def fetch_range(self, start: int, n: int) -> list[PersonModel]:
        records = await self._conn.fetch(SQL_RANGE, start, start + n - 1)
        return [PersonModel.model_construct(**r) for r in records]

    async def create_pool(self, dsn: str, size: int) -> _ConstructPool:
        return _ConstructPool(await asyncpg.create_pool(dsn, min_size=size, max_size=size))


DRIVERS = (
    AsyncSpec(name="asyncpg", factory=AsyncpgDriver, package="asyncpg", rows="native"),
    AsyncSpec(
        name="asyncpg-struct",
        factory=AsyncpgStructDriver,
        package="asyncpg",
        rows="struct",
        notes="Person(*record)",
    ),
    AsyncSpec(
        name="asyncpg-pydantic",
        factory=AsyncpgPydanticDriver,
        package="asyncpg",
        rows="pydantic",
        notes="PersonModel.model_validate(dict(record)) (validates)",
    ),
    AsyncSpec(
        name="asyncpg-pydantic-construct",
        factory=AsyncpgConstructDriver,
        package="asyncpg",
        rows="pydantic",
        notes="PersonModel.model_construct(**record) (no validation)",
    ),
)
