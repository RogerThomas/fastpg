"""`into=` a `NamedTuple`: column binding, type checks, NULLs, defaults, strategies."""

import datetime as dt
import gc
import pickle
import sys
import tracemalloc
import uuid
from decimal import Decimal
from typing import Any, Literal, NamedTuple, Self, assert_type

import pytest

import fastpg


class Everything(NamedTuple):
    b: bool
    i2: int
    i4: int
    i8: int
    f4: float
    f8: float
    s: str
    vc: str
    ch: str
    raw: bytes
    u: uuid.UUID
    d: dt.date
    t: dt.time
    ts: dt.datetime
    tstz: dt.datetime
    n: Decimal
    jb: dict[str, list[int]]
    j: list[Any]
    anything: Any


class NameAge(NamedTuple):
    name: str
    age: int


class NameAgeWithMethod(NameAge):
    """Adds a method but no field; ``__slots__ = ()`` keeps instances free of a ``__dict__``."""

    __slots__ = ()

    def label(self) -> str:
        return f"{self.name}-{self.age}"


class UpperName(NameAge):
    __slots__ = ()

    def __new__(cls, name: str, age: int) -> Self:
        return super().__new__(cls, name.upper(), age)


class NameBalance(NamedTuple):
    name: str
    balance: Decimal = Decimal(1)


class WithDefaults(NamedTuple):
    name: str
    age: int = 1
    email: str | None = "email"


class IdEmail(NamedTuple):
    id: int
    email: str | None


class NotNullEmail(NamedTuple):
    email: str


class Wrong(NamedTuple):
    name: int


class Score(NamedTuple):
    score: float


class JSONAsText(NamedTuple):
    doc: str


class Kind(NamedTuple):
    kind: Literal["a", "b"]


class Scalars(NamedTuple):
    id: int
    external_id: uuid.UUID
    balance: Decimal
    email: str | None


class ScalarsAndJSON(NamedTuple):
    id: int
    meta: dict[str, int]


class ScalarsWithDict(Scalars):
    """A subclass without ``__slots__``: instances have a ``__dict__``."""


class ScalarsWithNew(Scalars):
    __slots__ = ()

    def __new__(cls, id: int, external_id: uuid.UUID, balance: Decimal, email: str | None) -> Self:  # noqa: A002 - the field's name
        return super().__new__(cls, id, external_id, balance, email or "new")


async def test_every_column_type(conn: fastpg.Connection) -> None:
    sql = """
SELECT true AS b, 1::int2 AS i2, 2::int4 AS i4, 3::int8 AS i8, 1.5::float4 AS f4,
       2.5::float8 AS f8, 'text' AS s, 'varchar'::varchar AS vc, 'a'::"char" AS ch,
       '\\x0102'::bytea AS raw, 'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11'::uuid AS u,
       '2024-02-29'::date AS d, '12:34:56.789'::time AS t,
       '2024-02-29 12:34:56.789'::timestamp AS ts,
       '2024-02-29 12:34:56.789+00'::timestamptz AS tstz, 12.340::numeric AS n,
       '{"k": [1]}'::jsonb AS jb, '[1, "two"]'::json AS j, 4 AS anything
"""
    row = await conn.select_one(sql, into=Everything)
    assert_type(row, Everything)
    assert row == Everything(
        b=True,
        i2=1,
        i4=2,
        i8=3,
        f4=1.5,
        f8=2.5,
        s="text",
        vc="varchar",
        ch="a",
        raw=b"\x01\x02",
        u=uuid.UUID("a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11"),
        d=dt.date(2024, 2, 29),
        t=dt.time(12, 34, 56, 789_000),
        ts=dt.datetime(2024, 2, 29, 12, 34, 56, 789_000),  # noqa: DTZ001 - naive on purpose
        tstz=dt.datetime(2024, 2, 29, 12, 34, 56, 789_000, tzinfo=dt.UTC),
        n=Decimal("12.340"),
        jb={"k": [1]},
        j=[1, "two"],
        anything=4,
    )
    assert type(row) is Everything


async def test_column_order_independent(conn: fastpg.Connection) -> None:
    in_order = await conn.select_one("SELECT name, age FROM people WHERE id = 3", into=NameAge)
    reversed_ = await conn.select_one("SELECT age, name FROM people WHERE id = 3", into=NameAge)
    assert in_order == reversed_ == NameAge("person-3", 21)


async def test_extra_columns_ignored(conn: fastpg.Connection) -> None:
    got = await conn.select_one(
        "SELECT id, name, age, email FROM people WHERE id = 3",
        into=NameAge,
    )
    assert got == NameAge("person-3", 21)


async def test_missing_required_field(conn: fastpg.Connection) -> None:
    with pytest.raises(fastpg.MissingColumnError, match=r"NameAge requires field\(s\) age"):
        await conn.select_one("SELECT name FROM people WHERE id = 3", into=NameAge)


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT 'name' AS name", WithDefaults("name", 1, "email")),
        ("SELECT 'name' AS name, NULL AS email", WithDefaults("name", 1, None)),
        ("SELECT 2 AS age, 'name' AS name", WithDefaults("name", 2, "email")),
        ("SELECT 'e' AS email, 2 AS age, 'name' AS name", WithDefaults("name", 2, "e")),
    ],
)
async def test_missing_fields_with_defaults_use_them(
    conn: fastpg.Connection,
    sql: str,
    expected: WithDefaults,
) -> None:
    assert await conn.select_one(sql, into=WithDefaults) == expected


async def test_nulls(conn: fastpg.Connection) -> None:
    got = await conn.select_many(
        "SELECT id, email FROM people WHERE id IN (6, 7) ORDER BY id",
        into=IdEmail,
    )
    assert got == [IdEmail(6, "person6@example.com"), IdEmail(7, None)]


async def test_null_into_non_optional(conn: fastpg.Connection) -> None:
    with pytest.raises(fastpg.NullValueError, match=r'"email" is NULL but NotNullEmail\.email'):
        await conn.select_one("SELECT email FROM people WHERE id = 7", into=NotNullEmail)


async def test_type_mismatch(conn: fastpg.Connection) -> None:
    expected = r'"name" has PostgreSQL type text.*Wrong\.name expects int'
    with pytest.raises(fastpg.TypeMismatchError, match=expected):
        await conn.select_one("SELECT name FROM people WHERE id = 3", into=Wrong)
    with pytest.raises(fastpg.TypeMismatchError):
        await conn.select_many("SELECT name FROM people WHERE false", into=Wrong)


async def test_column_matching_a_field_twice(conn: fastpg.Connection) -> None:
    with pytest.raises(fastpg.TypeMismatchError, match=r'"age" appears more than once'):
        await conn.select_one("SELECT 'name' AS name, 1 AS age, 2 AS age", into=NameAge)


async def test_int_column_into_float_field(conn: fastpg.Connection) -> None:
    got = await conn.select_one("SELECT 5::int4 AS score", into=Score)
    assert got.score == 5.0
    assert type(got.score) is float


@pytest.mark.parametrize("json_type", ["json", "jsonb"])
async def test_str_field_gets_json_as_raw_text(conn: fastpg.Connection, json_type: str) -> None:
    got = await conn.select_one(f"""SELECT '{{"a": 1}}'::{json_type} AS doc""", into=JSONAsText)
    assert got == JSONAsText('{"a": 1}')


async def test_types_needing_validation_are_unsupported(conn: fastpg.Connection) -> None:
    with pytest.raises(fastpg.UnsupportedTypeError, match=r"Kind\.kind: LiteralType needs"):
        await conn.select_one("SELECT 'a' AS kind", into=Kind)
    with pytest.raises(fastpg.UnsupportedTypeError, match=r"strategy='direct' unavailable"):
        await conn.select_one("SELECT 'a' AS kind", into=Kind, strategy="direct")


@pytest.mark.parametrize("strategy", ["msgpack", "json"])
async def test_other_strategies_are_unsupported(
    conn: fastpg.Connection,
    strategy: fastpg.Strategy,
) -> None:
    with pytest.raises(fastpg.UnsupportedTypeError, match="NameAge is a NamedTuple"):
        await conn.select_one("SELECT 'name' AS name, 1 AS age", into=NameAge, strategy=strategy)


@pytest.mark.parametrize("into", [NameAge, NameAgeWithMethod, UpperName])
async def test_large_result_streams_in_chunks(
    conn: fastpg.Connection,
    into: type[NameAge],
) -> None:
    # Spans several 512-row chunks with a partial final chunk.
    sql = "SELECT name, age FROM people WHERE id <= 5000 ORDER BY id"
    rows = await conn.select_many(sql, into=into)
    assert rows == [into(*t) for t in await conn.select_many(sql)]
    assert len(rows) == 5000
    assert all(type(row) is into for row in rows)


async def test_rows_are_instances_of_the_class(conn: fastpg.Connection) -> None:
    row = await conn.select_one("SELECT name, age FROM people WHERE id = 3", into=NameAge)
    assert type(row) is NameAge
    assert isinstance(row, tuple)
    assert (row.name, row.age) == ("person-3", 21)
    assert (row[0], row[1], row[-1]) == ("person-3", 21, 21)
    assert len(row) == 2


async def test_rows_behave_like_constructed_instances(conn: fastpg.Connection) -> None:
    row = await conn.select_one("SELECT age, name FROM people WHERE id = 3", into=NameAge)
    constructed = NameAge("person-3", 21)
    assert row == constructed
    assert row == ("person-3", 21)
    assert row._replace(age=1) == NameAge("person-3", 1)
    assert type(row._replace(age=1)) is NameAge
    assert row._asdict() == {"name": "person-3", "age": 21}
    assert repr(row) == "NameAge(name='person-3', age=21)"
    assert hash(row) == hash(row) == hash(constructed) == hash(("person-3", 21))
    assert {row: 1}[constructed] == 1
    unpickled = pickle.loads(pickle.dumps(row))  # noqa: S301 - pickled just above
    assert unpickled == constructed
    assert type(unpickled) is NameAge


async def test_subclass_adding_a_method(conn: fastpg.Connection) -> None:
    row = await conn.select_one(
        "SELECT name, age FROM people WHERE id = 3",
        into=NameAgeWithMethod,
    )
    assert type(row) is NameAgeWithMethod
    assert row.label() == "person-3-21"
    assert row == NameAgeWithMethod("person-3", 21)
    assert hash(row) == hash(NameAgeWithMethod("person-3", 21))
    assert repr(row) == "NameAgeWithMethod(name='person-3', age=21)"
    assert type(row._replace(age=1)) is NameAgeWithMethod


async def test_custom_new_runs(conn: fastpg.Connection) -> None:
    row = await conn.select_one("SELECT age, name FROM people WHERE id = 3", into=UpperName)
    assert type(row) is UpperName
    assert row == UpperName("person-3", 21) == ("PERSON-3", 21)


async def test_defaults_and_nulls_in_many_rows(conn: fastpg.Connection) -> None:
    # Over one 512-row chunk: `age` comes from its default, `email` is NULL.
    sql = "SELECT name, NULL::text AS email FROM people WHERE id <= 600 ORDER BY id"
    rows = await conn.select_many(sql, into=WithDefaults)
    assert rows == [WithDefaults(f"person-{i}", 1, None) for i in range(1, 601)]


async def test_no_reference_leaks(conn: fastpg.Connection) -> None:
    sql = "SELECT name FROM people WHERE id <= 10000"
    await conn.select_many(sql, into=NameBalance)  # plans the type and prepares the statement
    # The connection holds on to the last result until the next query.
    await conn.select_one("SELECT 1")
    default = NameBalance._field_defaults["balance"]
    before = sys.getrefcount(default)
    rows = await conn.select_many(sql, into=NameBalance)
    await conn.select_one("SELECT 1")
    assert sys.getrefcount(default) == before + 10_000
    constructed = [NameBalance("".join(["person", "-1"]), Decimal(2))]
    assert sys.getrefcount(rows[0]) == sys.getrefcount(constructed[0])
    assert sys.getrefcount(rows[0].name) == sys.getrefcount(constructed[0].name)
    del rows
    assert sys.getrefcount(default) == before


async def test_rows_are_freed(conn: fastpg.Connection) -> None:
    sql = "SELECT name, age FROM people WHERE id <= 10000"
    await conn.select_many(sql, into=NameAge)  # plans the type and prepares the statement
    tracemalloc.start()
    try:
        for _ in range(3):
            assert len(await conn.select_many(sql, into=NameAge)) == 10_000
        # The connection holds on to the last result until the next query.
        await conn.select_one("SELECT 1")
        gc.collect()
        current, _ = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    # 10,000 leaked rows and names would be over 1 MB.
    assert current < 100_000


async def test_select_one_or_none(conn: fastpg.Connection) -> None:
    sql = "SELECT name, age FROM people WHERE id = $1"
    assert await conn.select_one_or_none(sql, -1, into=NameAge) is None
    assert await conn.select_one_or_none(sql, 3, into=NameAge) == NameAge("person-3", 21)


async def test_rows_of_scalars_are_not_gc_tracked(conn: fastpg.Connection) -> None:
    # Immutable, with no `__dict__`: like a tuple of scalars, nothing can make it part of a
    # cycle.
    sql = "SELECT id, external_id, balance, email FROM people WHERE id <= 3000 ORDER BY id"
    rows = await conn.select_many(sql, into=Scalars)
    gc.collect()
    assert not any(gc.is_tracked(row) for row in rows)
    assert rows[6] == Scalars(
        7,
        uuid.UUID("8f14e45f-ceea-167a-5a36-dedd4bea2543"),
        Decimal("2.59"),
        None,
    )


@pytest.mark.parametrize(
    ("into", "columns"),
    [
        (ScalarsAndJSON, """id, '{"k": 1}'::jsonb AS meta"""),
        (ScalarsWithDict, "id, external_id, balance, email"),
        (ScalarsWithNew, "id, external_id, balance, email"),
        (WithDefaults, "name"),
    ],
)
async def test_rows_that_could_hold_references_stay_gc_tracked(
    conn: fastpg.Connection,
    into: type[tuple[Any, ...]],
    columns: str,
) -> None:
    rows = await conn.select_many(f"SELECT {columns} FROM people WHERE id <= 3", into=into)  # noqa: S608
    assert [gc.is_tracked(row) for row in rows] == [True, True, True]


async def test_overridden_new_runs(conn: fastpg.Connection) -> None:
    sql = "SELECT id, external_id, balance, email FROM people WHERE id = 7"
    row = await conn.select_one(sql, into=ScalarsWithNew)
    assert row.email == "new"


async def test_in_a_transaction(conn: fastpg.Connection) -> None:
    async with conn.transaction() as tx:
        one = await tx.select_one("SELECT 'name' AS name, 1 AS age", into=NameAge)
        maybe = await tx.select_one_or_none(
            "SELECT 'name' AS name, 1 AS age WHERE false",
            into=NameAge,
        )
        many = await tx.select_many(
            "SELECT 'name' AS name, generate_series(1, 2) AS age",
            into=NameAge,
        )
    assert_type(one, NameAge)
    assert_type(maybe, NameAge | None)
    assert_type(many, list[NameAge])
    assert (one, maybe, many) == (
        NameAge("name", 1),
        None,
        [NameAge("name", 1), NameAge("name", 2)],
    )


async def test_through_a_pool(dsn: str) -> None:
    async with await fastpg.create_pool(dsn) as pool:
        one = await pool.select_one("SELECT 'name' AS name, 1 AS age", into=NameAge)
        maybe = await pool.select_one_or_none("SELECT 1 AS age, 'name' AS name", into=NameAge)
        many = await pool.select_many(
            "SELECT 'name' AS name, generate_series(1, 2) AS age",
            into=NameAge,
        )
        assert_type(one, NameAge)
        assert_type(maybe, NameAge | None)
        assert_type(many, list[NameAge])
        assert (one, maybe) == (NameAge("name", 1), NameAge("name", 1))
        assert many == [NameAge("name", 1), NameAge("name", 2)]
        async with pool.transaction() as tx:
            assert await tx.select_one("SELECT 'name' AS name, 1 AS age", into=NameAge) == NameAge(
                "name",
                1,
            )
