"""psycopg2 (psycopg2-binary wheel): libpq simple-query protocol, text results, ``%s``
placeholders interpolated client-side, no server-side prepared statements.
Default ``autocommit=False``: the first query opens a transaction that stays open.

Default rows are tuples, and ``uuid`` columns come back as ``str`` unless
``psycopg2.extras.register_uuid()`` is called. The struct variant registers it on its
connection (the documented way to get ``uuid.UUID``) and builds ``Person(*row)``.
"""

from typing import Any

import psycopg2
import psycopg2.extensions
import psycopg2.extras

from .base import Person, SyncSpec

SQL_ONE = (
    "SELECT id, name, age, email, score, balance, active, external_id, created_at, birthday "
    "FROM people WHERE id = %s"
)
SQL_RANGE = (
    "SELECT id, name, age, email, score, balance, active, external_id, created_at, birthday "
    "FROM people WHERE id BETWEEN %s AND %s"
)


class _Connection:
    _conn: psycopg2.extensions.connection

    def connect(self, dsn: str) -> None:
        self._conn = psycopg2.connect(dsn)

    def close(self) -> None:
        self._conn.close()


class Psycopg2Driver(_Connection):
    def fetch_one(self, person_id: int) -> tuple[Any, ...] | None:
        with self._conn.cursor() as cur:
            cur.execute(SQL_ONE, (person_id,))
            return cur.fetchone()

    def fetch_range(self, start: int, n: int) -> list[tuple[Any, ...]]:
        with self._conn.cursor() as cur:
            cur.execute(SQL_RANGE, (start, start + n - 1))
            return cur.fetchall()


class Psycopg2StructDriver(_Connection):
    def connect(self, dsn: str) -> None:
        super().connect(dsn)
        # types-psycopg2 leaves register_uuid's parameters and return unannotated.
        psycopg2.extras.register_uuid(conn_or_curs=self._conn)  # pyright: ignore[reportUnknownMemberType]

    def fetch_one(self, person_id: int) -> Person | None:
        with self._conn.cursor() as cur:
            cur.execute(SQL_ONE, (person_id,))
            row = cur.fetchone()
        return None if row is None else Person(*row)

    def fetch_range(self, start: int, n: int) -> list[Person]:
        with self._conn.cursor() as cur:
            cur.execute(SQL_RANGE, (start, start + n - 1))
            return [Person(*row) for row in cur.fetchall()]


DRIVERS = (
    SyncSpec(
        name="psycopg2",
        factory=Psycopg2Driver,
        package="psycopg2-binary",
        rows="native",
        notes="uuid as str",
    ),
    SyncSpec(
        name="psycopg2-struct",
        factory=Psycopg2StructDriver,
        package="psycopg2-binary",
        rows="struct",
        notes="register_uuid() + Person(*row)",
    ),
)
