"""Bulk writes: execute_many, insert_many and copy_in (binary COPY)."""

# ruff: noqa: S608 - SQL is built from fixture-generated table names

import asyncio
import datetime as dt
import enum
import uuid
from collections.abc import AsyncIterator, Iterable, Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, NamedTuple

import msgspec
import pytest
from pydantic import BaseModel, ConfigDict, Field

import fastpg
from fastpg import errors


class Mood(enum.Enum):
    HAPPY = "happy"
    SAD = "sad"


class SubUUID(uuid.UUID):
    """Subclasses take the generic path (exact types are read from their slots)."""


class SubDecimal(Decimal):
    pass


class Point(NamedTuple):
    id: int
    name: str
    price: float
    tags: list[str] | None


class Item(msgspec.Struct):
    id: int
    label: str = msgspec.field(name="name")
    price: Decimal | None = None
    tags: list[str] = msgspec.field(default_factory=list[str])


class Renamed(msgspec.Struct, rename="camel"):
    item_id: int
    display_name: str


class ItemModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: int
    label: str = Field(alias="name")
    price: Decimal | None = None


class Inner(msgspec.Struct):
    x: int


class Outer(msgspec.Struct):
    id: int
    nested: Inner
    many: list[Inner]


@dataclass
class TableMaker:
    """Creates fresh tables (`CREATE TABLE w_<n> (<columns>)`) and drops them afterwards."""

    _conn: fastpg.Connection
    _created: list[str] = field(default_factory=list[str])

    async def __call__(self, columns: str) -> str:
        table = f"w_{len(self._created)}"
        await self._conn.execute_script(
            f"DROP TABLE IF EXISTS {table}; CREATE TABLE {table} ({columns})",
        )
        self._created.append(table)
        return table

    async def drop_all(self) -> None:
        for table in self._created:
            await self._conn.execute_script(f"DROP TABLE IF EXISTS {table}")


@pytest.fixture
async def make_table(conn: fastpg.Connection) -> AsyncIterator[TableMaker]:
    await conn.execute_script(
        "DO $$ BEGIN CREATE TYPE mood AS ENUM ('sad', 'happy');"
        " EXCEPTION WHEN duplicate_object THEN NULL; END $$",
    )
    maker = TableMaker(conn)
    yield maker
    await maker.drop_all()


@pytest.fixture(params=["execute_many", "insert_many", "copy_in"])
def tier(request: pytest.FixtureRequest) -> str:
    """Every write API (execute_many gets an equivalent INSERT)."""
    return request.param


@pytest.fixture(params=["insert_many", "copy_in"])
def table_tier(request: pytest.FixtureRequest) -> str:
    """The write APIs that take a table and typed rows."""
    return request.param


async def write(
    conn: fastpg.Connection,
    tier: str,
    table: str | tuple[str, ...],
    rows: Iterable[Any],
    columns: list[str] | None = None,
) -> int:
    """Write `rows` with one of the three APIs."""
    if tier == "copy_in":
        return await conn.copy_in(table, rows, columns=columns)
    if tier == "insert_many":
        return await conn.insert_many(table, rows, columns=columns)
    assert isinstance(table, str)
    assert columns is not None
    placeholders = ", ".join(f"${i + 1}" for i in range(len(columns)))
    query = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
    return await conn.execute_many(query, rows)


def numbered_rows(count: int, started: asyncio.Event) -> Iterator[tuple[int, str]]:
    """`count` rows; sets `started` once 20,000 have been consumed."""
    for i in range(count):
        if i == 20_000:
            started.set()
        yield (i, "payload")


def timed_rows(count: int, start: dt.datetime) -> Iterator[tuple[int, str, float, dt.datetime]]:
    for i in range(count):
        yield (i, f"name-{i}", i / 4, start + dt.timedelta(seconds=i))


def case_id(value: object) -> str:
    return str(value) if isinstance(value, str) else type(value).__name__


# --- every supported type, NULLs ---------------------------------------------------------


@pytest.mark.parametrize(
    ("pg_type", "value", "expected"),
    [
        ("bool", True, True),
        ("bool", False, False),
        ("int2", -32768, -32768),
        ("int4", 2_147_483_647, 2_147_483_647),
        ("int8", -9_223_372_036_854_775_808, -9_223_372_036_854_775_808),
        ("float4", 1.5, 1.5),
        ("float8", 3.141592653589793, 3.141592653589793),
        ("float8", float("-inf"), float("-inf")),
        ("float8", 7, 7.0),
        ("text", "", ""),
        ("text", "téxt ✓", "téxt ✓"),
        ("varchar(10)", "varchar", "varchar"),
        ("char(4)", "ch", "ch  "),
        ("bytea", b"\x00\x01\xff", b"\x00\x01\xff"),
        ("bytea", bytearray(b"bytes"), b"bytes"),
        ("bytea", memoryview(b"bytes"), b"bytes"),
        ("uuid", uuid.UUID(int=1), uuid.UUID(int=1)),
        ("uuid", "00000000-0000-0000-0000-000000000002", uuid.UUID(int=2)),
        ("uuid", SubUUID(int=3), uuid.UUID(int=3)),
        ("date", dt.date(1, 1, 1), dt.date(1, 1, 1)),
        ("date", dt.date(9999, 12, 31), dt.date(9999, 12, 31)),
        ("time", dt.time(23, 59, 59, 999_999), dt.time(23, 59, 59, 999_999)),
        (
            "timestamp",
            dt.datetime(2000, 1, 1, 0, 0, 0, 1),  # noqa: DTZ001 - naive on purpose
            dt.datetime(2000, 1, 1, 0, 0, 0, 1),  # noqa: DTZ001
        ),
        (
            "timestamptz",
            dt.datetime(2000, 1, 1, 12, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30))),
            dt.datetime(2000, 1, 1, 6, 30, tzinfo=dt.UTC),
        ),
        (
            "timestamptz",
            dt.datetime(1999, 12, 31, 23, 59, 59, 999_999, tzinfo=dt.UTC),
            dt.datetime(1999, 12, 31, 23, 59, 59, 999_999, tzinfo=dt.UTC),
        ),
        ("numeric", Decimal("-10000.0005"), Decimal("-10000.0005")),
        ("numeric", Decimal("1E+3"), Decimal(1000)),
        ("numeric", Decimal("-1.5E-7"), Decimal("-0.00000015")),
        ("numeric", Decimal("0E-8"), Decimal("0.00000000")),
        ("numeric", Decimal("-Infinity"), Decimal("-Infinity")),
        ("numeric", SubDecimal("2.5"), Decimal("2.5")),
        (
            "numeric",
            Decimal("123456789012345678901234567890.123456789"),
            Decimal("123456789012345678901234567890.123456789"),
        ),
        ("numeric", 42, Decimal(42)),
        ("numeric", -(2**70), Decimal(-(2**70))),
        ("float8", 2**70, 2.0**70),
        ("numeric", "1.25", Decimal("1.25")),
        ("numeric(12, 2)", Decimal("1.50"), Decimal("1.50")),
        ("jsonb", {"key": [1, 2.5, None, True]}, {"key": [1, 2.5, None, True]}),
        ("jsonb", '{"key": "value"}', {"key": "value"}),
        ("json", [1, "value"], [1, "value"]),
        ("mood", Mood.SAD, "sad"),
        ("mood", "happy", "happy"),
    ],
    ids=case_id,
)
async def test_type_roundtrip(
    conn: fastpg.Connection,
    make_table: TableMaker,
    tier: str,
    pg_type: str,
    value: object,
    expected: object,
) -> None:
    table = await make_table(f"i int, v {pg_type}")
    n = await write(conn, tier, table, [(0, value), (1, None)], ["i", "v"])
    assert n == 2
    got = await conn.select_many(f"SELECT v FROM {table} ORDER BY i")
    assert got == [(expected,), (None,)]
    assert type(got[0][0]) is type(expected)


async def test_value_errors_name_row_and_column(
    conn: fastpg.Connection,
    make_table: TableMaker,
    tier: str,
) -> None:
    table = await make_table("i int, v int2")
    with pytest.raises(
        fastpg.ParameterError,
        match=r"row 2, .*out of range for PostgreSQL type int2",
    ) as exc:
        await write(conn, tier, table, [(0, 1), (1, 2), (2, 70_000)], ["i", "v"])
    assert exc.value.row_index == 2
    with pytest.raises(
        fastpg.ParameterError,
        match=r"row 0, .*cannot encode str as PostgreSQL type int4",
    ):
        await write(conn, tier, table, [("i", 1)], ["i", "v"])


async def test_float_beyond_float4_range_is_out_of_range(
    conn: fastpg.Connection,
    make_table: TableMaker,
    tier: str,
) -> None:
    table = await make_table("i int, v float4")
    with pytest.raises(
        fastpg.ParameterError,
        match=r"row 1, .*float out of range for PostgreSQL type float4",
    ):
        await write(conn, tier, table, [(0, 1.5), (1, 1e300)], ["i", "v"])
    tz_table = await make_table("t timestamptz")
    with pytest.raises(fastpg.ParameterError, match="naive datetime"):
        await write(conn, tier, tz_table, [(dt.datetime(2000, 1, 1),)], ["t"])  # noqa: DTZ001
    naive_table = await make_table("t timestamp")
    with pytest.raises(fastpg.ParameterError, match="aware datetime"):
        await write(conn, tier, naive_table, [(dt.datetime(2000, 1, 1, tzinfo=dt.UTC),)], ["t"])
    date_table = await make_table("d date")
    with pytest.raises(fastpg.ParameterError, match=r"cannot encode aware datetime\.datetime as"):
        await write(conn, tier, date_table, [(dt.datetime(2000, 1, 1, tzinfo=dt.UTC),)], ["d"])
    assert await conn.select_one(f"SELECT count(*) FROM {table}") == (0,)


# --- row kinds, renamed fields / aliases --------------------------------------------------


async def test_struct_rows_use_encoded_names(
    conn: fastpg.Connection,
    make_table: TableMaker,
    table_tier: str,
) -> None:
    table = await make_table("id int PRIMARY KEY, name text, price numeric, tags jsonb")
    rows = [Item(1, "label-1", Decimal("1.50"), ["tag"]), Item(2, "label-2")]
    assert await write(conn, table_tier, table, rows) == 2
    assert await conn.select_many(f"SELECT * FROM {table} ORDER BY id", into=Item) == rows
    renamed = await make_table('"itemId" int, "displayName" text')
    assert await write(conn, table_tier, renamed, [Renamed(1, "display-name")]) == 1
    got = await conn.select_many(f"SELECT * FROM {renamed}", into=Renamed)
    assert got == [Renamed(1, "display-name")]


async def test_pydantic_rows_use_aliases(
    conn: fastpg.Connection,
    make_table: TableMaker,
    table_tier: str,
) -> None:
    table = await make_table("id int PRIMARY KEY, name text, price numeric")
    rows = [ItemModel(id=1, name="label-1", price=Decimal("2.5")), ItemModel(id=2, name="label-2")]
    assert await write(conn, table_tier, table, rows) == 2
    got = await conn.select_many(f"SELECT * FROM {table} ORDER BY id", into=ItemModel)
    assert got == rows


async def test_mapping_and_sequence_rows(
    conn: fastpg.Connection,
    make_table: TableMaker,
    table_tier: str,
) -> None:
    table = await make_table(
        "id int PRIMARY KEY, name text, price numeric, tags jsonb DEFAULT '[]'",
    )
    assert (
        await write(conn, table_tier, table, [{"id": 1, "name": "a"}, {"name": "b", "id": 2}]) == 2
    )
    sequences: list[Any] = [
        (3, "c", None, ["tag"]),
        [4, "d", Decimal(1), []],
        Point(5, "e", 1.5, None),
    ]
    assert await write(conn, table_tier, table, sequences) == 3
    # columns= selects (and orders) the columns; extra mapping keys are then ignored.
    picked = [{"name": "f", "id": 6, "ignored": object()}]
    assert await write(conn, table_tier, table, picked, ["name", "id"]) == 1
    assert await write(conn, table_tier, table, [Item(7, "g", tags=["tag"])], ["id", "name"]) == 1
    got = await conn.select_many(f"SELECT id, name, price, tags FROM {table} ORDER BY id")
    assert got == [
        (1, "a", None, []),
        (2, "b", None, []),
        (3, "c", None, ["tag"]),
        (4, "d", Decimal(1), []),
        (5, "e", Decimal("1.5"), None),
        (6, "f", None, []),
        (7, "g", None, []),
    ]


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        (
            [{"id": 1, "name": "a"}, {"id": 2, "name": "b", "extra": 1}],
            r'unexpected key\(s\) \["extra"\]',
        ),
        ([{"id": 1, "name": "a"}, {"id": 2}], "missing key"),
        ([Item(1, "a"), {"id": 2, "name": "b"}], "expected Item, got dict"),
        ([(1, "a", None, None), (2, "b")], "expected 4 values, got 2"),
        ([(1, "a", None, None), "abcd"], "expected a sequence of values, got str"),
    ],
    ids=["extra-key", "missing-key", "mixed-types", "short-tuple", "str-row"],
)
async def test_row_shape_errors(
    conn: fastpg.Connection,
    make_table: TableMaker,
    table_tier: str,
    rows: list[Any],
    message: str,
) -> None:
    table = await make_table("id int PRIMARY KEY, name text, price numeric, tags jsonb")
    with pytest.raises(fastpg.ParameterError, match=message) as exc:
        await write(conn, table_tier, table, rows)
    assert exc.value.row_index == 1
    assert await conn.select_one(f"SELECT count(*) FROM {table}") == (0,)


async def test_unusable_rows_and_columns(
    conn: fastpg.Connection,
    make_table: TableMaker,
    table_tier: str,
) -> None:
    table = await make_table("id int, name text")
    with pytest.raises(TypeError, match="rows must be msgspec Structs"):
        await write(conn, table_tier, table, [object()])
    with pytest.raises(ValueError, match="no field for column"):
        await write(conn, table_tier, table, [Item(1, "a")], ["column"])


async def test_json_columns_take_nested_structs(
    conn: fastpg.Connection,
    make_table: TableMaker,
) -> None:
    table = await make_table("id int, nested jsonb, many json")
    rows = [Outer(1, Inner(2), [Inner(3)])]
    assert await conn.copy_in(table, rows) == 1
    assert await conn.insert_many(table, rows) == 1
    assert await conn.select_many(f"SELECT * FROM {table}", into=Outer) == rows * 2


async def test_domain_columns(conn: fastpg.Connection, make_table: TableMaker, tier: str) -> None:
    await conn.execute_script(
        "DROP DOMAIN IF EXISTS w_positive CASCADE;"
        " CREATE DOMAIN w_positive AS int CONSTRAINT w_positive_check CHECK (VALUE > 0)",
    )
    table = await make_table("i int, v w_positive")
    assert await write(conn, tier, table, [(0, 5)], ["i", "v"]) == 1
    with pytest.raises(errors.CheckViolationError) as exc:
        await write(conn, tier, table, [(1, 1), (2, -1)], ["i", "v"])
    assert exc.value.row_index == 1
    assert await conn.select_many(f"SELECT v FROM {table}") == [(5,)]


# --- identifiers ------------------------------------------------------------------------


async def test_odd_identifiers_are_quoted(conn: fastpg.Connection, table_tier: str) -> None:
    await conn.execute_script(
        'DROP SCHEMA IF EXISTS "we.ird ""schema" CASCADE; CREATE SCHEMA "we.ird ""schema";'
        ' CREATE TABLE "we.ird ""schema"."Odd ""Tab"" le" ("Mixed Case" int, "has""quote" text,'
        ' "select" bool)',
    )
    table = ('we.ird "schema', 'Odd "Tab" le')
    rows = [{"Mixed Case": 1, 'has"quote': "quote", "select": True}]
    assert await write(conn, table_tier, table, rows) == 1
    assert (
        await write(
            conn,
            table_tier,
            table,
            [(2, "quote", False)],
            ["Mixed Case", 'has"quote', "select"],
        )
        == 1
    )
    got = await conn.select_many('SELECT * FROM "we.ird ""schema"."Odd ""Tab"" le" ORDER BY 1')
    assert got == [(1, "quote", True), (2, "quote", False)]
    await conn.execute_script('DROP SCHEMA "we.ird ""schema" CASCADE')


async def test_schema_qualified_names(conn: fastpg.Connection) -> None:
    await conn.execute_script(
        "DROP SCHEMA IF EXISTS w_schema CASCADE; CREATE SCHEMA w_schema;"
        " CREATE TABLE w_schema.t (a int)",
    )
    assert await conn.insert_many("w_schema.t", [(1,)]) == 1
    assert await conn.copy_in(("w_schema", "t"), [(2,)]) == 1
    assert await conn.select_many("SELECT a FROM w_schema.t ORDER BY a") == [(1,), (2,)]
    # Quoted, hence exact: "T" is not t.
    with pytest.raises(errors.UndefinedTableError):
        await conn.insert_many("w_schema.T", [(3,)])
    with pytest.raises(ValueError, match="invalid SQL identifier"):
        await conn.copy_in("w_schema..t", [(3,)])
    await conn.execute_script("DROP SCHEMA w_schema CASCADE")


# --- atomicity and errors ---------------------------------------------------------------


async def test_database_error_is_atomic_with_row_index(
    conn: fastpg.Connection,
    make_table: TableMaker,
    tier: str,
) -> None:
    table = await make_table("id int CONSTRAINT w_pk PRIMARY KEY, v text")
    await write(conn, tier, table, [(1, "v")], ["id", "v"])
    rows = [(i, "v") for i in range(2, 3000)]
    rows[2500] = (1, "v")
    with pytest.raises(fastpg.UniqueViolationError) as exc:
        await write(conn, tier, table, rows, ["id", "v"])
    assert exc.value.constraint_name == "w_pk"
    if tier == "insert_many":
        # 64 rows per INSERT statement: the server only says which statement failed.
        assert exc.value.row_index is None
        assert any("rows 2496..2559" in note for note in exc.value.__notes__)
    else:
        assert exc.value.row_index == 2500
        assert any("row 2500" in note for note in exc.value.__notes__)
    assert await conn.select_many(f"SELECT id FROM {table}") == [(1,)]
    assert await conn.select_one("SELECT 1") == (1,)


async def test_encoding_error_late_in_input_rolls_back(
    conn: fastpg.Connection,
    make_table: TableMaker,
    tier: str,
) -> None:
    """Earlier chunks were already sent when a later row fails to encode."""
    table = await make_table("id int, v int4")
    rows: list[tuple[int, object]] = [(i, i) for i in range(20_000)]
    rows[15_000] = (15_000, "v")
    with pytest.raises(fastpg.ParameterError) as exc:
        await write(conn, tier, table, rows, ["id", "v"])
    assert exc.value.row_index == 15_000
    assert await conn.select_one(f"SELECT count(*) FROM {table}") == (0,)


async def test_deferred_constraint_fails_at_commit(
    conn: fastpg.Connection,
    make_table: TableMaker,
) -> None:
    table = await make_table("id int UNIQUE DEFERRABLE INITIALLY DEFERRED")
    with pytest.raises(fastpg.UniqueViolationError) as exc:
        await conn.execute_many(f"INSERT INTO {table} VALUES ($1)", [(1,), (2,), (1,)])
    assert exc.value.row_index is None  # raised by COMMIT, not by a row
    assert await conn.select_one(f"SELECT count(*) FROM {table}") == (0,)


async def test_execute_many_counts_and_statement_errors(
    conn: fastpg.Connection,
    make_table: TableMaker,
) -> None:
    table = await make_table("id int, v int")
    rows = [(i,) for i in range(10)]
    assert await conn.execute_many(f"INSERT INTO {table} VALUES ($1, 0)", rows) == 10
    updated = await conn.execute_many(
        f"UPDATE {table} SET v = v + $2 WHERE id < $1",
        [(5, 1), (2, 10), (100, 0)],
    )
    assert updated == 5 + 2 + 10
    assert await conn.select_one(f"SELECT sum(v) FROM {table}") == (5 + 20,)
    with pytest.raises(fastpg.ParameterError, match=r"row 0: expected 2 values, got 1"):
        await conn.execute_many(f"UPDATE {table} SET v = $2 WHERE id = $1", [(1,)])
    with pytest.raises(errors.SQLSyntaxError):
        await conn.execute_many("INSERT INTO nowhere VALUES (", [(1,)])
    with pytest.raises(errors.UndefinedColumnError):
        await conn.copy_in(table, [(1,)], columns=["column"])
    assert await conn.select_one("SELECT 1") == (1,)


@pytest.mark.parametrize("failing_row", [0, 1, 1022, 1023, 1024, 2047, 2999])
async def test_execute_many_names_the_failing_row_anywhere_in_a_chunk(
    conn: fastpg.Connection,
    make_table: TableMaker,
    failing_row: int,
) -> None:
    """A chunk's rows share one Sync; the failing one is still named exactly."""
    table = await make_table("id int PRIMARY KEY")
    await conn.execute(f"INSERT INTO {table} VALUES (0)")
    rows = [(i,) for i in range(1, 3001)]
    rows[failing_row] = (0,)
    with pytest.raises(fastpg.UniqueViolationError) as exc:
        await conn.execute_many(f"INSERT INTO {table} VALUES ($1)", rows)
    assert exc.value.row_index == failing_row
    assert await conn.select_one(f"SELECT count(*) FROM {table}") == (1,)
    assert await conn.execute_many(f"INSERT INTO {table} VALUES ($1)", rows[5:10]) == 5


async def test_insert_many_names_the_failing_statement_at_a_chunk_boundary(
    conn: fastpg.Connection,
    make_table: TableMaker,
) -> None:
    table = await make_table("id int PRIMARY KEY")
    rows = [(i,) for i in range(3000)]
    rows[1023] = (0,)  # the last 64-row statement of the first chunk
    with pytest.raises(fastpg.UniqueViolationError) as exc:
        await conn.insert_many(table, rows)
    assert exc.value.row_index is None
    assert any("rows 960..1023" in note for note in exc.value.__notes__)
    rows[1023] = (1023,)
    rows[2999] = (0,)  # 952 rows in the last chunk: 14 statements of 64, then one per row
    with pytest.raises(fastpg.UniqueViolationError) as exc:
        await conn.insert_many(table, rows)
    assert exc.value.row_index == 2999
    assert await conn.select_one(f"SELECT count(*) FROM {table}") == (0,)


async def test_execute_many_sums_each_statements_count(
    conn: fastpg.Connection,
    make_table: TableMaker,
) -> None:
    table = await make_table("id int, v int")
    await conn.copy_in(table, [(i, 0) for i in range(3000)])
    bounds = [(k, k + k % 4) for k in range(3000)]  # each UPDATE touches 0 to 3 rows
    updated = await conn.execute_many(
        f"UPDATE {table} SET v = v + 1 WHERE id >= $1 AND id < $2",
        bounds,
    )
    expected = sum(min(k % 4, 3000 - k) for k in range(3000))
    assert updated == expected
    assert await conn.select_one(f"SELECT sum(v) FROM {table}") == (expected,)
    deleted = await conn.execute_many(f"DELETE FROM {table} WHERE id = $1", [(1,), (1,), (2,)])
    assert deleted == 2


async def test_execute_many_returning_counts_rows(
    conn: fastpg.Connection,
    make_table: TableMaker,
) -> None:
    table = await make_table("id int")
    rows = [(i,) for i in range(2500)]
    assert await conn.execute_many(f"INSERT INTO {table} VALUES ($1) RETURNING id", rows) == 2500


async def test_execute_many_with_notices_and_parameter_status(
    conn: fastpg.Connection,
    make_table: TableMaker,
) -> None:
    """Asynchronous messages between the statements' results don't upset the counts."""
    table = await make_table("id int PRIMARY KEY")
    await conn.execute_script(
        "CREATE OR REPLACE FUNCTION w_notice() RETURNS trigger LANGUAGE plpgsql AS"
        " $$ BEGIN RAISE NOTICE 'row %', NEW.id; RETURN NEW; END $$;"
        f" CREATE TRIGGER w_notice BEFORE INSERT ON {table}"
        " FOR EACH ROW EXECUTE FUNCTION w_notice()",
    )
    rows = [(i,) for i in range(2000)]
    assert await conn.execute_many(f"INSERT INTO {table} VALUES ($1)", rows) == 2000
    with pytest.raises(fastpg.UniqueViolationError) as exc:
        await conn.execute_many(f"INSERT INTO {table} VALUES ($1)", [(2000,), (2001,), (0,)])
    assert exc.value.row_index == 2
    names = [(f"name-{i}",) for i in range(1500)]
    assert (
        await conn.execute_many("SELECT set_config('application_name', $1, false)", names) == 1500
    )
    assert await conn.select_one("SELECT current_setting('application_name')") == ("name-1499",)
    await conn.execute_script(f"DROP TABLE {table}; DROP FUNCTION w_notice()")


async def test_execute_many_counts_survive_statement_closes(
    dsn: str,
    make_table: TableMaker,
) -> None:
    """Evicted cached statements are closed (one-way) while bulk writes run."""
    table = await make_table("id int")
    rows = [(i,) for i in range(3000)]
    async with await fastpg.connect(dsn, statement_cache_size=1) as conn:
        for i in range(20):
            results = await asyncio.gather(
                conn.select_one(f"SELECT {i}::int"),
                conn.select_one(f"SELECT {i}::int + 1"),
                conn.execute_many(f"INSERT INTO {table} VALUES ($1)", rows),
                conn.select_one(f"SELECT {i}::int + 2"),
            )
            assert results == [(i,), (i + 1,), 3000, (i + 2,)]
        assert await conn.select_one(f"SELECT count(*) FROM {table}") == (60_000,)


async def test_copy_that_fails_to_start_keeps_connection_in_sync(
    conn: fastpg.Connection,
    make_table: TableMaker,
) -> None:
    """The COPY itself is rejected after rows were already pipelined behind it."""
    table = await make_table("id int")
    await conn.execute_script(f"CREATE OR REPLACE VIEW w_view AS SELECT id FROM {table}")
    with pytest.raises(fastpg.DatabaseError, match="view"):
        await conn.copy_in("w_view", [(i,) for i in range(10_000)])
    assert await conn.select_many("SELECT n FROM generate_series(1, 3) n") == [(1,), (2,), (3,)]
    await conn.execute_script("DROP VIEW w_view")


async def test_empty_input_sends_nothing(conn: fastpg.Connection) -> None:
    assert await conn.execute_many("not even sql", []) == 0
    assert await conn.insert_many("no_such_table", iter([])) == 0
    assert await conn.copy_in("no_such_table", ()) == 0


# --- streaming, transactions, concurrency, cancellation ---------------------------------


async def test_large_copy_streams_from_a_generator(
    conn: fastpg.Connection,
    make_table: TableMaker,
) -> None:
    table = await make_table("id int8, name text, score float8, at timestamptz")
    start = dt.datetime(2000, 1, 1, tzinfo=dt.UTC)
    assert await conn.copy_in(table, timed_rows(200_000, start)) == 200_000
    got = await conn.select_one(
        f"SELECT count(*), sum(id), max(name), sum(score), max(at) FROM {table}",
    )
    assert got == (
        200_000,
        Decimal(200_000 * 199_999 // 2),
        "name-99999",
        sum(i / 4 for i in range(200_000)),
        start + dt.timedelta(seconds=199_999),
    )


async def test_execute_many_in_a_transaction_block_then_copy_in(
    conn: fastpg.Connection,
    make_table: TableMaker,
) -> None:
    table = await make_table("id int, v text")
    async with conn.transaction() as tx:
        assert (
            await tx.execute_many(f"INSERT INTO {table} VALUES ($1, $2)", [(1, "a"), (2, "b")]) == 2
        )
    assert await conn.copy_in(table, [(i, str(i)) for i in range(3, 10_003)]) == 10_000
    assert await conn.select_one(f"SELECT count(*), max(id) FROM {table}") == (10_002, 10_002)


async def test_inside_an_open_transaction(
    conn: fastpg.Connection,
    make_table: TableMaker,
    tier: str,
) -> None:
    table = await make_table("id int PRIMARY KEY")
    await conn.execute("BEGIN")
    await conn.execute(f"INSERT INTO {table} VALUES (0)")
    # Success joins the caller's transaction (no COMMIT of its own) ...
    assert await write(conn, tier, table, [(1,), (2,)], ["id"]) == 2
    # ... and a failure rolls back only its own rows: the transaction stays usable.
    with pytest.raises(fastpg.UniqueViolationError) as exc:
        await write(conn, tier, table, [(3,), (4,), (2,)], ["id"])
    assert exc.value.row_index == 2
    assert await conn.select_many(f"SELECT id FROM {table} ORDER BY id") == [(0,), (1,), (2,)]
    await conn.execute("ROLLBACK")
    assert await conn.select_one(f"SELECT count(*) FROM {table}") == (0,)


async def test_in_failed_transaction(conn: fastpg.Connection, make_table: TableMaker) -> None:
    table = await make_table("id int")
    await conn.execute("BEGIN")
    with pytest.raises(errors.DataError):
        await conn.execute("SELECT 1 / 0")
    with pytest.raises(fastpg.DatabaseError) as exc:
        await conn.copy_in(table, [(1,)])
    assert exc.value.sqlstate == "25P02"  # in_failed_sql_transaction
    await conn.execute("ROLLBACK")
    assert await conn.copy_in(table, [(1,)]) == 1


async def test_concurrent_queries_wait_for_the_bulk_write(
    conn: fastpg.Connection,
    make_table: TableMaker,
    tier: str,
) -> None:
    table = await make_table("id int")
    count_sql = f"SELECT count(*) FROM {table}"
    results = await asyncio.gather(
        write(conn, tier, table, [(i,) for i in range(30_000)], ["id"]),
        *(conn.select_one(count_sql) for _ in range(5)),
        conn.select_one("SELECT 42"),
    )
    assert results[0] == 30_000
    # Each count ran entirely before or after the write: never a partial view.
    assert all(r in {(0,), (30_000,)} for r in results[1:-1])
    assert results[-1] == (42,)


@pytest.mark.parametrize("in_transaction", [False, True], ids=["autocommit", "in-transaction"])
async def test_cancellation_rolls_back_and_keeps_connection_usable(
    conn: fastpg.Connection,
    make_table: TableMaker,
    tier: str,
    in_transaction: bool,  # noqa: FBT001
) -> None:
    table = await make_table("id int, payload text")
    if in_transaction:
        await conn.execute("BEGIN")
        await conn.execute(f"INSERT INTO {table} VALUES (-1, 'kept')")
    started = asyncio.Event()
    task = asyncio.create_task(
        write(conn, tier, table, numbered_rows(400_000, started), ["id", "payload"]),
    )
    await started.wait()  # ~20k rows handed over, the rest not produced yet
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await conn.select_one("SELECT 1") == (1,)
    expected = 1 if in_transaction else 0
    assert await conn.select_one(f"SELECT count(*) FROM {table}") == (expected,)
    if in_transaction:
        await conn.execute("COMMIT")
    assert await write(conn, tier, table, [(1, "payload")], ["id", "payload"]) == 1


async def test_closed_connection(dsn: str) -> None:
    conn = await fastpg.connect(dsn)
    await conn.close()
    with pytest.raises(fastpg.ConnectionClosedError):
        await conn.copy_in("table", [(1,)])
    with pytest.raises(fastpg.ConnectionClosedError):
        await conn.execute_many("SELECT $1::int", [(1,)])
