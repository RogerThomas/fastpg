"""Shared contract for benchmark driver adapters.

An adapter wraps one Postgres client library and exposes the handful of operations the
scenarios need. Each driver module under ``bench/drivers/`` defines its adapters and a
module-level ``DRIVERS`` tuple of :class:`AsyncSpec` / :class:`SyncSpec`; the registry in
``bench/drivers/__init__.py`` discovers every module automatically.

Every adapter runs the same SQL (all ten ``people`` columns) in the driver's own
placeholder style, and returns rows either in the driver's default row type or as
:class:`Person` structs (the ``-struct`` variants).
"""

import datetime as dt
import decimal
import hashlib
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

import msgspec
import pydantic

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

DEFAULT_DSN = "postgresql://fastpg:fastpg@127.0.0.1:55432/fastpg"
TABLE_ROWS = 100_000  # people.id runs 1..TABLE_ROWS, see docker/initdb/02-seed.sql


class Person(msgspec.Struct):
    """One ``people`` row, the shape fastpg decodes into natively."""

    id: int
    name: str
    age: int
    email: str | None
    score: float
    balance: decimal.Decimal
    active: bool
    external_id: uuid.UUID
    created_at: dt.datetime
    birthday: dt.date | None


class AsyncPool(Protocol):
    """A driver-owned connection pool used by the concurrent scenario."""

    async def fetch_one(self, person_id: int) -> object:
        """Acquire a connection, fetch one row by primary key, release the connection."""
        ...

    async def close(self) -> None:
        """Close the pool and all its connections."""
        ...


class AsyncDriver(Protocol):
    """Adapter for an asyncio driver. One instance holds one connection."""

    async def connect(self, dsn: str) -> None:
        """Open the single connection used by the sequential scenarios."""
        ...

    async def fetch_one(self, person_id: int) -> object:
        """Fetch the row with ``id = person_id`` (always exists)."""
        ...

    async def fetch_range(self, start: int, n: int) -> Sequence[object]:
        """Fetch the ``n`` rows with ``id BETWEEN start AND start + n - 1``."""
        ...

    async def close(self) -> None:
        """Close the connection opened by :meth:`connect`."""
        ...

    async def create_pool(self, dsn: str, size: int) -> AsyncPool:
        """Create (and open) a pool of exactly ``size`` connections."""
        ...


class SyncDriver(Protocol):
    """Adapter for a blocking driver. One instance holds one connection."""

    def connect(self, dsn: str) -> None:
        """Open the single connection used by the scenarios."""
        ...

    def fetch_one(self, person_id: int) -> object:
        """Fetch the row with ``id = person_id`` (always exists)."""
        ...

    def fetch_range(self, start: int, n: int) -> Sequence[object]:
        """Fetch the ``n`` rows with ``id BETWEEN start AND start + n - 1``."""
        ...

    def close(self) -> None:
        """Close the connection opened by :meth:`connect`."""
        ...


type RowShape = Literal["native", "struct", "pydantic"]


class PersonModel(pydantic.BaseModel):
    """The same row as a pydantic v2 model (the ``-pydantic`` variants)."""

    id: int
    name: str
    age: int
    email: str | None
    score: float
    balance: decimal.Decimal
    active: bool
    external_id: uuid.UUID
    created_at: dt.datetime
    birthday: dt.date | None


def as_person(model: PersonModel) -> Person:
    """Convert for verification against :func:`expected_person`."""
    return Person(**model.model_dump())


@dataclass(frozen=True, slots=True, kw_only=True)
class AsyncSpec:
    """Registry entry for an async adapter."""

    name: str
    factory: Callable[[], AsyncDriver]
    package: str  # distribution name, for version reporting
    rows: RowShape
    notes: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class SyncSpec:
    """Registry entry for a sync adapter."""

    name: str
    factory: Callable[[], SyncDriver]
    package: str
    rows: RowShape
    notes: str = ""


type DriverSpec = AsyncSpec | SyncSpec
type Kind = Literal["async", "sync"]


def kind_of(spec: DriverSpec) -> Kind:
    return "async" if isinstance(spec, AsyncSpec) else "sync"


_EPOCH = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
_BIRTHDAY_BASE = dt.date(1950, 1, 1)


def expected_person(i: int) -> Person:
    """Recompute row ``i`` from the seed formulas in ``docker/initdb/02-seed.sql``."""
    return Person(
        id=i,
        name=f"person-{i}",
        age=18 + i % 60,
        email=None if i % 7 == 0 else f"person{i}@example.com",
        score=(i % 1000) / 10.0,
        balance=decimal.Decimal((i * 37) % 100_000).scaleb(-2),
        active=i % 2 == 0,
        external_id=uuid.UUID(hashlib.md5(str(i).encode(), usedforsecurity=False).hexdigest()),
        created_at=_EPOCH + dt.timedelta(seconds=i * 61),
        birthday=None if i % 11 == 0 else _BIRTHDAY_BASE + dt.timedelta(days=i % 20_000),
    )
