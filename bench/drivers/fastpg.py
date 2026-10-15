"""fastpg: binary protocol, ``$n`` placeholders, LRU cache of named prepared statements.

Default rows are tuples; the struct variants build ``Person`` directly in Rust
(``strategy="direct"``) or via MessagePack + a cached ``msgspec.msgpack.Decoder``
(``strategy="msgpack"``).

The ``concurrent`` scenario goes through ``fastpg.create_pool`` (``min_size`` = ``max_size``
= N) in shared mode, like an application would; the ``-pipelined`` variant instead serves
all N tasks from one shared, pipelined connection.
"""

from typing import TYPE_CHECKING

import fastpg

from .base import AsyncSpec, Person, PersonModel

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    type _Target = fastpg.Pool | fastpg.Connection
    type _Close = Callable[[], Awaitable[None]]

SQL_ONE = (
    "SELECT id, name, age, email, score, balance, active, external_id, created_at, birthday "
    "FROM people WHERE id = $1"
)
SQL_RANGE = (
    "SELECT id, name, age, email, score, balance, active, external_id, created_at, birthday "
    "FROM people WHERE id BETWEEN $1 AND $2"
)


class _Pool:
    """Shared-mode queries on a ``fastpg.Pool``, or on one pipelined connection."""

    def __init__(
        self,
        target: _Target,
        close: _Close,
        strategy: fastpg.Strategy | None,
    ) -> None:
        self._target = target
        self._close = close
        self._strategy: fastpg.Strategy | None = strategy

    async def fetch_one(self, person_id: int) -> object:
        if self._strategy is None:
            return await self._target.select_one_or_none(SQL_ONE, person_id)
        return await self._target.select_one_or_none(
            SQL_ONE,
            person_id,
            into=Person,
            strategy=self._strategy,
        )

    async def close(self) -> None:
        await self._close()


class _Driver:
    strategy: fastpg.Strategy | None = None
    pipelined = False
    _conn: fastpg.Connection

    async def _open_shared(self, dsn: str, size: int) -> tuple[_Target, _Close]:
        """The pool's query target and its closer: a real pool, or one pipelined connection."""
        if self.pipelined:
            conn = await fastpg.connect(dsn)
            return conn, conn.close
        pool = await fastpg.create_pool(dsn, min_size=size, max_size=size)
        return pool, pool.close

    async def connect(self, dsn: str) -> None:
        self._conn = await fastpg.connect(dsn)

    async def close(self) -> None:
        await self._conn.close()

    async def create_pool(self, dsn: str, size: int) -> _Pool:
        return _Pool(*await self._open_shared(dsn, size), self.strategy)


class FastpgDriver(_Driver):
    async def fetch_one(self, person_id: int) -> tuple[object, ...] | None:
        return await self._conn.select_one_or_none(SQL_ONE, person_id)

    async def fetch_range(self, start: int, n: int) -> list[tuple[object, ...]]:
        return await self._conn.select_many(SQL_RANGE, start, start + n - 1)


class FastpgStructDriver(_Driver):
    strategy: fastpg.Strategy | None = "direct"

    async def fetch_one(self, person_id: int) -> Person | None:
        return await self._conn.select_one_or_none(
            SQL_ONE,
            person_id,
            into=Person,
            strategy=self.strategy or "direct",
        )

    async def fetch_range(self, start: int, n: int) -> list[Person]:
        return await self._conn.select_many(
            SQL_RANGE,
            start,
            start + n - 1,
            into=Person,
            strategy=self.strategy or "direct",
        )


class _PydanticPool(_Pool):
    async def fetch_one(self, person_id: int) -> object:
        strategy = self._strategy or "direct"
        return await self._target.select_one_or_none(
            SQL_ONE,
            person_id,
            into=PersonModel,
            strategy=strategy,
        )


class FastpgPydanticDriver(_Driver):
    strategy: fastpg.Strategy | None = "direct"

    async def fetch_one(self, person_id: int) -> PersonModel | None:
        return await self._conn.select_one_or_none(
            SQL_ONE,
            person_id,
            into=PersonModel,
            strategy=self.strategy or "direct",
        )

    async def fetch_range(self, start: int, n: int) -> list[PersonModel]:
        return await self._conn.select_many(
            SQL_RANGE,
            start,
            start + n - 1,
            into=PersonModel,
            strategy=self.strategy or "direct",
        )

    async def create_pool(self, dsn: str, size: int) -> _PydanticPool:
        return _PydanticPool(*await self._open_shared(dsn, size), self.strategy)


class FastpgPydanticJsonDriver(FastpgPydanticDriver):
    strategy: fastpg.Strategy | None = "json"


class FastpgJsonDriver(FastpgStructDriver):
    strategy: fastpg.Strategy | None = "json"


class FastpgMsgpackDriver(FastpgStructDriver):
    strategy: fastpg.Strategy | None = "msgpack"


class FastpgPipelinedDriver(FastpgStructDriver):
    pipelined = True


DRIVERS = (
    AsyncSpec(name="fastpg", factory=FastpgDriver, package="fastpg", rows="native"),
    AsyncSpec(
        name="fastpg-struct",
        factory=FastpgStructDriver,
        package="fastpg",
        rows="struct",
        notes="direct Struct construction in Rust",
    ),
    AsyncSpec(
        name="fastpg-msgpack",
        factory=FastpgMsgpackDriver,
        package="fastpg",
        rows="struct",
        notes="rows -> msgpack -> cached msgspec Decoder",
    ),
    AsyncSpec(
        name="fastpg-pipelined",
        factory=FastpgPipelinedDriver,
        package="fastpg",
        rows="struct",
        notes="as fastpg-struct, but `concurrent` shares ONE pipelined connection, no pool",
    ),
    AsyncSpec(
        name="fastpg-json",
        factory=FastpgJsonDriver,
        package="fastpg",
        rows="struct",
        notes="rows -> JSON -> cached msgspec.json Decoder",
    ),
    AsyncSpec(
        name="fastpg-pydantic",
        factory=FastpgPydanticDriver,
        package="fastpg",
        rows="pydantic",
        notes="native model_construct-equivalent (schema proves validation is a no-op)",
    ),
    AsyncSpec(
        name="fastpg-pydantic-json",
        factory=FastpgPydanticJsonDriver,
        package="fastpg",
        rows="pydantic",
        notes="rows -> JSON -> TypeAdapter(list[PersonModel]).validate_json (validates)",
    ),
)
