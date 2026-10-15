"""pg8000 (pure Python), via its recommended ``pg8000.native`` API.

Text format for both parameters and results, ``:name`` placeholders. Every ``run()`` with
parameters goes through an *unnamed* statement: Parse + Describe + Bind + Execute on each
call, with no statement caching. Autocommit (no implicit transaction).

Default rows are lists; the struct variant builds ``Person(*row)``.
"""

from typing import Any, Protocol, cast
from urllib.parse import unquote, urlsplit

import pg8000.native  # pyright: ignore[reportMissingTypeStubs] (pg8000 ships no hints)

from .base import Person, SyncSpec

SQL_ONE = (
    "SELECT id, name, age, email, score, balance, active, external_id, created_at, birthday "
    "FROM people WHERE id = :id"
)
SQL_RANGE = (
    "SELECT id, name, age, email, score, balance, active, external_id, created_at, birthday "
    "FROM people WHERE id BETWEEN :lo AND :hi"
)


class _NativeConnection(Protocol):
    """The slice of ``pg8000.native.Connection`` used here (pg8000 ships no type hints)."""

    def run(self, sql: str, **params: object) -> list[list[Any]]: ...

    def close(self) -> None: ...


def _connect(dsn: str) -> _NativeConnection:
    """pg8000 takes keyword arguments rather than a DSN/URL."""
    url = urlsplit(dsn)
    conn = pg8000.native.Connection(
        user=unquote(url.username or ""),
        password=unquote(url.password) if url.password else None,
        host=url.hostname or "localhost",
        port=url.port or 5432,
        database=url.path.removeprefix("/") or None,
    )
    # pg8000 is untyped; narrow it to the methods we call.
    return cast(_NativeConnection, conn)


class _Connection:
    _conn: _NativeConnection

    def connect(self, dsn: str) -> None:
        self._conn = _connect(dsn)

    def close(self) -> None:
        self._conn.close()


class Pg8000Driver(_Connection):
    def fetch_one(self, person_id: int) -> list[Any]:
        return self._conn.run(SQL_ONE, id=person_id)[0]

    def fetch_range(self, start: int, n: int) -> list[list[Any]]:
        return self._conn.run(SQL_RANGE, lo=start, hi=start + n - 1)


class Pg8000StructDriver(_Connection):
    def fetch_one(self, person_id: int) -> Person:
        return Person(*self._conn.run(SQL_ONE, id=person_id)[0])

    def fetch_range(self, start: int, n: int) -> list[Person]:
        return [Person(*row) for row in self._conn.run(SQL_RANGE, lo=start, hi=start + n - 1)]


DRIVERS = (
    SyncSpec(name="pg8000", factory=Pg8000Driver, package="pg8000", rows="native"),
    SyncSpec(
        name="pg8000-struct",
        factory=Pg8000StructDriver,
        package="pg8000",
        rows="struct",
        notes="Person(*row)",
    ),
)
