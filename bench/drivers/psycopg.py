"""psycopg 3 sync ``Connection`` (C "binary" implementation, psycopg-binary wheel).

Text result format, ``%s`` placeholders bound server-side (extended protocol). A query is
prepared server-side automatically after ``prepare_threshold`` (default 5) executions.
Default ``autocommit=False``: the first query opens a transaction that stays open.

Default rows are tuples; the struct variant uses ``row_factory=args_row(Person)``.
"""

from psycopg import Connection
from psycopg.rows import TupleRow, args_row

from .base import Person, SyncSpec

SQL_ONE = (
    "SELECT id, name, age, email, score, balance, active, external_id, created_at, birthday "
    "FROM people WHERE id = %s"
)
SQL_RANGE = (
    "SELECT id, name, age, email, score, balance, active, external_id, created_at, birthday "
    "FROM people WHERE id BETWEEN %s AND %s"
)


class PsycopgDriver:
    _conn: Connection[TupleRow]

    def connect(self, dsn: str) -> None:
        self._conn = Connection.connect(dsn)

    def fetch_one(self, person_id: int) -> TupleRow | None:
        return self._conn.execute(SQL_ONE, (person_id,)).fetchone()

    def fetch_range(self, start: int, n: int) -> list[TupleRow]:
        return self._conn.execute(SQL_RANGE, (start, start + n - 1)).fetchall()

    def close(self) -> None:
        self._conn.close()


class PsycopgStructDriver:
    _conn: Connection[Person]

    def connect(self, dsn: str) -> None:
        self._conn = Connection[Person].connect(dsn, row_factory=args_row(Person))

    def fetch_one(self, person_id: int) -> Person | None:
        return self._conn.execute(SQL_ONE, (person_id,)).fetchone()

    def fetch_range(self, start: int, n: int) -> list[Person]:
        return self._conn.execute(SQL_RANGE, (start, start + n - 1)).fetchall()

    def close(self) -> None:
        self._conn.close()


DRIVERS = (
    SyncSpec(name="psycopg", factory=PsycopgDriver, package="psycopg", rows="native"),
    SyncSpec(
        name="psycopg-struct",
        factory=PsycopgStructDriver,
        package="psycopg",
        rows="struct",
        notes="row_factory=args_row(Person)",
    ),
)
