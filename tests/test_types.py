"""Value round trips: Python param → PostgreSQL → Python, for every supported type."""

import datetime as dt
import enum
import gc
import uuid
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import msgspec
import pytest

import fastpg


@pytest.mark.parametrize(
    ("pg_type", "value"),
    [
        ("bool", True),
        ("bool", False),
        ("int2", -32768),
        ("int4", 2_147_483_647),
        ("int8", -9_223_372_036_854_775_808),
        ("float4", 1.5),
        ("float8", 3.141592653589793),
        ("float8", float("inf")),
        ("text", ""),
        ("text", "héllo wörld ✓"),
        ("varchar", "abc"),
        ("bytea", b"\x00\x01\xff"),
        ("bytea", b""),
        ("uuid", uuid.UUID("28dd2c79-55ce-9264-5624-0b2ff0100bde")),
        ("date", dt.date(2000, 1, 1)),
        ("date", dt.date(1, 1, 1)),
        ("date", dt.date(9999, 12, 31)),
        ("date", dt.date(1969, 7, 20)),
        ("time", dt.time(0, 0)),
        ("time", dt.time(23, 59, 59, 999_999)),
        ("timestamp", dt.datetime(1999, 12, 31, 23, 59, 59, 999_999)),  # noqa: DTZ001 - naive on purpose
        ("timestamp", dt.datetime(2024, 2, 29, 12, 0, 0, 1)),  # noqa: DTZ001
        ("timestamptz", dt.datetime(2024, 1, 1, 0, 1, 1, tzinfo=dt.UTC)),
        ("timestamptz", dt.datetime(1900, 6, 1, 8, 30, tzinfo=dt.UTC)),
        ("numeric", Decimal("0")),
        ("numeric", Decimal("28.49")),
        ("numeric", Decimal("-10000.0005")),
        ("numeric", Decimal("0.00000005")),
        ("numeric", Decimal("123456789012345678901234567890.123456789")),
        ("numeric", Decimal("NaN")),
        ("jsonb", {"a": [1, 2.5, None, True], "b": {"c": "d"}}),
        ("json", [1, "two"]),
    ],
)
async def test_roundtrip(conn: fastpg.Connection, pg_type: str, value: object) -> None:
    (got,) = await conn.select_one(f"SELECT $1::{pg_type}", value)
    if isinstance(value, Decimal) and value.is_nan():
        assert isinstance(got, Decimal)
        assert got.is_nan()
    else:
        assert got == value
        assert type(got) is type(value)


async def test_null_for_every_type(conn: fastpg.Connection) -> None:
    types = ["bool", "bytea", "date", "float4", "float8", "int2", "int4", "int8", "json", "jsonb"]
    types += ["numeric", "text", "time", "timestamp", "timestamptz", "uuid", "varchar"]
    row = await conn.select_one("SELECT " + ", ".join(f"NULL::{t}" for t in types))
    assert row == (None,) * len(types)


class Color(enum.Enum):
    RED = "red"


class Key(uuid.UUID):
    """A `uuid.UUID` subclass: converted through `isinstance`, not by its exact type."""


async def test_every_parameter_type_in_one_statement(conn: fastpg.Connection) -> None:
    params = (
        None,
        True,
        7,
        "text",
        1.5,
        b"\x01",
        uuid.UUID(int=1),
        Key(int=2),
        bytearray(b"\x02"),
        memoryview(b"\x03"),
        dt.datetime(2024, 1, 1, 12, tzinfo=dt.UTC),
        dt.datetime(2024, 1, 1, 12),  # noqa: DTZ001 - naive on purpose
        dt.date(2024, 1, 1),
        dt.time(12, 0, 0, 1),
        Decimal("1.5"),
        Color.RED,
        {"k": 1},
        [1],
    )
    query = (
        "SELECT $1::int4, $2::bool, $3::int8, $4::text, $5::float8, $6::bytea, $7::uuid,"
        " $8::uuid, $9::bytea, $10::bytea, $11::timestamptz, $12::timestamp, $13::date,"
        " $14::time, $15::numeric, $16::text, $17::jsonb, $18::jsonb"
    )
    assert await conn.select_one(query, *params) == (
        None,
        True,
        7,
        "text",
        1.5,
        b"\x01",
        uuid.UUID(int=1),
        uuid.UUID(int=2),
        b"\x02",
        b"\x03",
        dt.datetime(2024, 1, 1, 12, tzinfo=dt.UTC),
        dt.datetime(2024, 1, 1, 12),  # noqa: DTZ001
        dt.date(2024, 1, 1),
        dt.time(12, 0, 0, 1),
        Decimal("1.5"),
        "red",
        {"k": 1},
        [1],
    )


@pytest.mark.parametrize(
    "value",
    [
        dt.datetime(1, 1, 1, tzinfo=dt.UTC),
        dt.datetime(9999, 12, 31, 23, 59, 59, 999_999, tzinfo=dt.UTC),
        dt.datetime(1999, 12, 31, 23, 59, 59, 999_999, tzinfo=dt.UTC),
        dt.datetime(2000, 1, 1, 0, 0, 0, 1, tzinfo=dt.UTC),
        # Europe/London: the last instant before BST starts, the first one after it, and
        # both 01:30s of the night BST ends (fold 0 is BST, fold 1 is GMT).
        dt.datetime(2024, 3, 31, 0, 59, 59, 999_999, tzinfo=ZoneInfo("Europe/London")),
        dt.datetime(2024, 3, 31, 2, 0, tzinfo=ZoneInfo("Europe/London")),
        dt.datetime(2024, 10, 27, 1, 30, fold=0, tzinfo=ZoneInfo("Europe/London")),
        dt.datetime(2024, 10, 27, 1, 30, fold=1, tzinfo=ZoneInfo("Europe/London")),
    ],
)
async def test_aware_datetime_at_range_dst_and_microsecond_boundaries(
    conn: fastpg.Connection,
    value: dt.datetime,
) -> None:
    (got,) = await conn.select_one("SELECT $1::timestamptz", value)
    assert got == value.astimezone(dt.UTC)
    assert got.tzinfo is dt.UTC
    assert got.microsecond == value.microsecond


async def test_naive_datetime_at_range_boundaries(conn: fastpg.Connection) -> None:
    low = dt.datetime(1, 1, 1)  # noqa: DTZ001 - naive on purpose
    high = dt.datetime(9999, 12, 31, 23, 59, 59, 999_999)  # noqa: DTZ001
    assert await conn.select_one("SELECT $1::timestamp, $2::timestamp", low, high) == (low, high)


async def test_aware_datetime_converted_to_utc(conn: fastpg.Connection) -> None:
    tz = dt.timezone(dt.timedelta(hours=5, minutes=30))
    (got,) = await conn.select_one("SELECT $1::timestamptz", dt.datetime(2024, 1, 1, 12, tzinfo=tz))
    assert got == dt.datetime(2024, 1, 1, 6, 30, tzinfo=dt.UTC)
    assert got.tzinfo is dt.UTC


async def test_numeric_scale_preserved(conn: fastpg.Connection) -> None:
    (got,) = await conn.select_one("SELECT 1.50::numeric(10, 2)")
    assert str(got) == "1.50"


async def test_numeric_infinity(conn: fastpg.Connection) -> None:
    assert await conn.select_one("SELECT 'Infinity'::numeric, '-Infinity'::numeric") == (
        Decimal("Infinity"),
        Decimal("-Infinity"),
    )


async def test_int_param_widens_to_float_and_numeric(conn: fastpg.Connection) -> None:
    assert await conn.select_one("SELECT $1::float8, $2::numeric", 3, 7) == (3.0, Decimal(7))


async def test_str_params_for_uuid_numeric_json(conn: fastpg.Connection) -> None:
    u = "28dd2c79-55ce-9264-5624-0b2ff0100bde"
    got = await conn.select_one("SELECT $1::uuid, $2::numeric, $3::jsonb", u, "1.25", '{"a":1}')
    assert got == (uuid.UUID(u), Decimal("1.25"), {"a": 1})


async def test_uuid_instances_behave_like_uuids(conn: fastpg.Connection) -> None:
    (got,) = await conn.select_one("SELECT 'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11'::uuid")
    expected = uuid.UUID("a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11")
    assert got == expected
    assert hash(got) == hash(expected)
    assert got.version == 4
    assert got.is_safe is uuid.SafeUUID.unknown
    assert str(got) == str(expected)
    untyped_caller: Any = got  # what an untyped caller could do
    with pytest.raises(TypeError):
        untyped_caller.int = 5


async def test_uuids_and_decimals_are_not_gc_tracked(conn: fastpg.Connection) -> None:
    # They can't be part of a reference cycle, so the cyclic GC needn't traverse them.
    query = """
SELECT 'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11'::uuid, 1.5::numeric, 'NaN'::numeric,
       '-10000.0005'::numeric, NULL::uuid, NULL::numeric
FROM generate_series(1, 2000)
"""
    rows = await conn.select_many(query)
    gc.collect()
    assert {gc.is_tracked(value) for row in rows for value in row} == {False}
    u, n, nan, negative, null_u, null_n = rows[-1]
    assert (u, n, negative) == (
        uuid.UUID("a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11"),
        Decimal("1.5"),
        Decimal("-10000.0005"),
    )
    assert nan.is_nan()
    assert (null_u, null_n) == (None, None)


async def test_tuple_rows_of_scalars_are_not_gc_tracked(conn: fastpg.Connection) -> None:
    query = "SELECT id, external_id, balance, email FROM people WHERE id <= 2000 ORDER BY id"
    rows = await conn.select_many(query)
    assert not any(gc.is_tracked(row) for row in rows)
    assert rows[6] == (7, uuid.UUID("8f14e45f-ceea-167a-5a36-dedd4bea2543"), Decimal("2.59"), None)
    single = await conn.select_one("SELECT 1, 'text'")
    assert not gc.is_tracked(single)


async def test_tuple_rows_with_json_stay_gc_tracked(conn: fastpg.Connection) -> None:
    # A json value can be a container; the row tuple must stay visible to the cyclic GC.
    rows = await conn.select_many("""SELECT 1, '{"k": [1]}'::jsonb FROM generate_series(1, 3)""")
    assert [gc.is_tracked(row) for row in rows] == [True, True, True]
    assert rows[0] == (1, {"k": [1]})


async def test_enum_and_text_like_types(conn: fastpg.Connection) -> None:
    await conn.execute_script(
        "DROP TYPE IF EXISTS mood CASCADE; CREATE TYPE mood AS ENUM ('sad', 'happy');",
    )
    assert await conn.select_one("SELECT 'happy'::mood, 'x'::name, 'ab'::char(4)") == (
        "happy",
        "x",
        "ab  ",
    )
    (label,) = await conn.select_one("SELECT $1::mood", "sad")
    assert label == "sad"


async def test_internal_char_type(conn: fastpg.Connection) -> None:
    # '\000' is the byte 0, whose text output is empty.
    query = """SELECT 'a'::"char", '\\000'::"char", NULL::"char" """
    assert await conn.select_one(query) == ("a", "", None)


async def test_internal_char_from_the_catalog(conn: fastpg.Connection) -> None:
    (relkind,) = await conn.select_one("SELECT relkind FROM pg_class WHERE relname = 'pg_type'")
    assert relkind == "r"


@pytest.mark.parametrize("value", ["a", "", "é"])
async def test_internal_char_param_roundtrip(conn: fastpg.Connection, value: str) -> None:
    assert await conn.select_one('SELECT $1::"char"', value) == (value,)


@pytest.mark.parametrize("value", ["ab", "✓"])
async def test_internal_char_param_rejects_more_than_one_byte(
    conn: fastpg.Connection,
    value: str,
) -> None:
    with pytest.raises(fastpg.ParameterError, match=r'\$1: "char" takes a str'):
        await conn.select_one('SELECT $1::"char"', value)


async def test_pg_type_query_with_internal_char_columns(conn: fastpg.Connection) -> None:
    query = """
select typname, typnamespace, typowner, typlen, typbyval, typcategory, typispreferred,
       typisdefined, typdelim, typrelid, typelem, typarray
from pg_type where typtypmod = $1 and typisdefined = $2
"""
    rows = await conn.select_many(query, *(-1, True))
    assert rows
    assert {len(row[5]) for row in rows} == {1}


async def test_infinite_date_is_a_decode_error(conn: fastpg.Connection) -> None:
    with pytest.raises(fastpg.DecodeError, match="infinite"):
        await conn.select_one("SELECT 'infinity'::date")


async def test_unsupported_type(conn: fastpg.Connection) -> None:
    with pytest.raises(fastpg.UnsupportedTypeError, match=r"interval\[\]"):
        await conn.select_one("SELECT ARRAY['1 day'::interval]")


async def test_unsupported_param_type(conn: fastpg.Connection) -> None:
    with pytest.raises(fastpg.ParameterError, match=r"\$1: unsupported type object"):
        await conn.select_one("SELECT $1::text", object())


class Everything(msgspec.Struct):
    b: bool
    i: int
    f: float
    s: str
    raw: bytes
    u: uuid.UUID
    d: dt.date
    t: dt.time
    ts: dt.datetime
    tstz: dt.datetime
    n: Decimal
    j: dict[str, Any]


async def test_direct_and_msgpack_agree_on_every_type(conn: fastpg.Connection) -> None:
    query = """
SELECT true AS b, 42 AS i, 1.5::float8 AS f, 'x' AS s, '\\x0102'::bytea AS raw,
       'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11'::uuid AS u, '2024-02-29'::date AS d,
       '12:34:56.789'::time AS t, '2024-02-29 12:34:56.789'::timestamp AS ts,
       '2024-02-29 12:34:56.789+00'::timestamptz AS tstz, 12.340::numeric AS n,
       '{"k": [1, {"z": null}]}'::jsonb AS j
"""
    direct = await conn.select_one(query, into=Everything, strategy="direct")
    via_msgpack = await conn.select_one(query, into=Everything, strategy="msgpack")
    assert direct == via_msgpack
    assert direct.n == Decimal("12.340")
    assert direct.ts.tzinfo is None
    assert direct.tstz.tzinfo is not None
