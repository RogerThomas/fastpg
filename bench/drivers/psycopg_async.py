"""psycopg 3 ``AsyncConnection`` (C "binary" implementation, psycopg-binary wheel).

Text result format, ``%s`` placeholders bound server-side (extended protocol). A query is
prepared server-side automatically after ``prepare_threshold`` (default 5) executions.
Default ``autocommit=False``: the first query opens a transaction that stays open; each
pooled ``connection()`` block commits on exit (BEGIN + query + COMMIT per op).

Default rows are tuples; the struct variant uses ``row_factory=args_row(Person)``.
"""

from psycopg import AsyncConnection
from psycopg.rows import TupleRow, args_row, class_row
from psycopg_pool import AsyncConnectionPool

from .base import AsyncSpec, Person, PersonModel

SQL_ONE = (
    "SELECT id, name, age, email, score, balance, active, external_id, created_at, birthday "
    "FROM people WHERE id = %s"
)
SQL_RANGE = (
    "SELECT id, name, age, email, score, balance, active, external_id, created_at, birthday "
    "FROM people WHERE id BETWEEN %s AND %s"
)


type _PoolT = AsyncConnectionPool[AsyncConnection[TupleRow]]


async def _open_pool(dsn: str, size: int) -> _PoolT:
    pool: _PoolT = AsyncConnectionPool(dsn, min_size=size, max_size=size, open=False)
    await pool.open(wait=True)
    return pool


class _Pool:
    def __init__(self, pool: _PoolT) -> None:
        self._pool = pool

    async def close(self) -> None:
        await self._pool.close()


class _TuplePool(_Pool):
    async def fetch_one(self, person_id: int) -> TupleRow | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(SQL_ONE, (person_id,))
            return await cur.fetchone()


class _StructPool(_Pool):
    async def fetch_one(self, person_id: int) -> Person | None:
        async with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=args_row(Person))
            await cur.execute(SQL_ONE, (person_id,))
            return await cur.fetchone()


class _ModelPool(_Pool):
    async def fetch_one(self, person_id: int) -> PersonModel | None:
        async with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=class_row(PersonModel))
            await cur.execute(SQL_ONE, (person_id,))
            return await cur.fetchone()


class PsycopgAsyncDriver:
    _conn: AsyncConnection[TupleRow]

    async def connect(self, dsn: str) -> None:
        self._conn = await AsyncConnection.connect(dsn)

    async def fetch_one(self, person_id: int) -> TupleRow | None:
        cur = await self._conn.execute(SQL_ONE, (person_id,))
        return await cur.fetchone()

    async def fetch_range(self, start: int, n: int) -> list[TupleRow]:
        cur = await self._conn.execute(SQL_RANGE, (start, start + n - 1))
        return await cur.fetchall()

    async def close(self) -> None:
        await self._conn.close()

    async def create_pool(self, dsn: str, size: int) -> _TuplePool:
        return _TuplePool(await _open_pool(dsn, size))


class PsycopgAsyncStructDriver:
    _conn: AsyncConnection[Person]

    async def connect(self, dsn: str) -> None:
        self._conn = await AsyncConnection[Person].connect(dsn, row_factory=args_row(Person))

    async def fetch_one(self, person_id: int) -> Person | None:
        cur = await self._conn.execute(SQL_ONE, (person_id,))
        return await cur.fetchone()

    async def fetch_range(self, start: int, n: int) -> list[Person]:
        cur = await self._conn.execute(SQL_RANGE, (start, start + n - 1))
        return await cur.fetchall()

    async def close(self) -> None:
        await self._conn.close()

    async def create_pool(self, dsn: str, size: int) -> _StructPool:
        return _StructPool(await _open_pool(dsn, size))


class PsycopgAsyncPydanticDriver:
    _conn: AsyncConnection[PersonModel]

    async def connect(self, dsn: str) -> None:
        self._conn = await AsyncConnection[PersonModel].connect(
            dsn,
            row_factory=class_row(PersonModel),
        )

    async def fetch_one(self, person_id: int) -> PersonModel | None:
        cur = await self._conn.execute(SQL_ONE, (person_id,))
        return await cur.fetchone()

    async def fetch_range(self, start: int, n: int) -> list[PersonModel]:
        cur = await self._conn.execute(SQL_RANGE, (start, start + n - 1))
        return await cur.fetchall()

    async def close(self) -> None:
        await self._conn.close()

    async def create_pool(self, dsn: str, size: int) -> _ModelPool:
        return _ModelPool(await _open_pool(dsn, size))


DRIVERS = (
    AsyncSpec(
        name="psycopg-async",
        factory=PsycopgAsyncDriver,
        package="psycopg",
        rows="native",
    ),
    AsyncSpec(
        name="psycopg-async-struct",
        factory=PsycopgAsyncStructDriver,
        package="psycopg",
        rows="struct",
        notes="row_factory=args_row(Person)",
    ),
    AsyncSpec(
        name="psycopg-async-pydantic",
        factory=PsycopgAsyncPydanticDriver,
        package="psycopg",
        rows="pydantic",
        notes="row_factory=class_row(PersonModel) (validates)",
    ),
)
