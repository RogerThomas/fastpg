"""Parameters that don't fit the type the server inferred for them."""

import datetime as dt
import math
from decimal import Decimal

import pytest

import fastpg


class Opaque:
    """Not serialisable as JSON."""


def _array_of_parameters(count: int) -> str:
    """A query taking `count` int parameters (an array, so the parse tree stays flat)."""
    return "SELECT cardinality(ARRAY[" + ",".join(f"${i}::int" for i in range(1, count + 1)) + "])"


async def test_65535_parameters(conn: fastpg.Connection) -> None:
    assert await conn.select_one(_array_of_parameters(65_535), *[1] * 65_535) == (65_535,)


@pytest.mark.parametrize("count", [65_536, 70_000])
async def test_more_than_65535_parameters_are_refused_before_preparing(
    conn: fastpg.Connection,
    count: int,
) -> None:
    query = "SELECT count(*) FROM pg_prepared_statements"
    (prepared,) = await conn.select_one(query)
    expected = rf"^PostgreSQL takes at most 65535 parameters per statement, got {count}$"
    with pytest.raises(fastpg.ParameterError, match=expected):
        await conn.select_one(_array_of_parameters(count), *[1] * count)
    assert await conn.select_one(query) == (prepared,)  # nothing left on the server


@pytest.mark.parametrize("value", [2**64, -(2**70), 10**100])
async def test_int_beyond_64_bits_for_numeric(conn: fastpg.Connection, value: int) -> None:
    assert await conn.select_one("SELECT $1::numeric", value) == (Decimal(value),)


async def test_int_beyond_64_bits_for_float(conn: fastpg.Connection) -> None:
    assert await conn.select_one("SELECT $1::float8, $1::float4", 2**70) == (2.0**70, 2.0**70)


@pytest.mark.parametrize(
    ("pg_type", "value"),
    [("int8", 2**63), ("int8", -(2**63) - 1), ("int4", 2**64), ("oid", 2**64)],
)
async def test_int_beyond_64_bits_for_an_integer_type_is_out_of_range(
    conn: fastpg.Connection,
    pg_type: str,
    value: int,
) -> None:
    expected = rf"^parameter \$1: int out of range for PostgreSQL type {pg_type}$"
    with pytest.raises(fastpg.ParameterError, match=expected):
        await conn.select_one(f"SELECT $1::{pg_type}", value)


async def test_int_beyond_float_range_is_out_of_range(conn: fastpg.Connection) -> None:
    with pytest.raises(fastpg.ParameterError, match="int out of range for PostgreSQL type float8"):
        await conn.select_one("SELECT $1::float8", 10**400)


@pytest.mark.parametrize("value", [1e300, -1e300, 1e-50])
async def test_float_beyond_float4_range_is_out_of_range(
    conn: fastpg.Connection,
    value: float,
) -> None:
    # As PostgreSQL's own float8 -> float4 cast: not rounded to infinity or zero.
    expected = r"^parameter \$1: float out of range for PostgreSQL type float4$"
    with pytest.raises(fastpg.ParameterError, match=expected):
        await conn.select_one("SELECT $1::float4", value)


@pytest.mark.parametrize("value", [0.0, -0.0, 1.5, 1e-40, math.inf, -math.inf])
async def test_float_within_float4_range(conn: fastpg.Connection, value: float) -> None:
    (got,) = await conn.select_one("SELECT $1::float4", value)
    assert got == pytest.approx(value, rel=1e-6)
    assert math.copysign(1, got) == math.copysign(1, value)


async def test_float_nan_for_float4(conn: fastpg.Connection) -> None:
    (got,) = await conn.select_one("SELECT $1::float4", math.nan)
    assert math.isnan(got)


async def test_int_with_more_digits_than_python_converts(conn: fastpg.Connection) -> None:
    with pytest.raises(fastpg.ParameterError, match=r"^parameter \$1: int too large to encode"):
        await conn.select_one("SELECT $1::numeric", 10**5000)


@pytest.mark.parametrize(
    ("query", "value", "cause"),
    [
        ("SELECT $1::text", "\ud800", UnicodeEncodeError),
        ("SELECT $1::jsonb", {"key": "\ud800"}, UnicodeEncodeError),
        ("SELECT $1::jsonb", {"key": Opaque()}, TypeError),
    ],
)
async def test_unencodable_python_value_is_a_parameter_error(
    conn: fastpg.Connection,
    query: str,
    value: object,
    cause: type[Exception],
) -> None:
    with pytest.raises(fastpg.ParameterError, match=rf"^parameter \$1: {cause.__name__}: ") as exc:
        await conn.select_one(query, value)
    assert isinstance(exc.value.__cause__, cause)
    assert await conn.select_one("SELECT 1") == (1,)


@pytest.mark.parametrize(
    ("pg_type", "value", "described"),
    [
        ("int4", "1", "str"),
        ("int4", 1.0, "float"),
        ("int4", Decimal(1), "decimal.Decimal"),
        ("text", 1, "int"),
        ("uuid", 1, "int"),
        ("date", dt.datetime(2024, 1, 1), "naive datetime.datetime"),  # noqa: DTZ001
        ("timestamp", dt.date(2024, 1, 1), "datetime.date"),
        ("bytea", "a", "str"),
        ("jsonb", b"{}", "bytes"),
        ("bool", 1, "int"),
    ],
)
async def test_value_of_the_wrong_type_names_the_parameter_and_both_types(
    conn: fastpg.Connection,
    pg_type: str,
    value: object,
    described: str,
) -> None:
    expected = rf"^parameter \$2: cannot encode {described} as PostgreSQL type {pg_type}$"
    with pytest.raises(fastpg.ParameterError, match=expected):
        await conn.select_one(f"SELECT $1::int4, $2::{pg_type}", 1, value)
    assert await conn.select_one("SELECT 1") == (1,)


@pytest.mark.parametrize("pg_type", ["int2", "int4", "int8", "oid", "numeric", "float8"])
@pytest.mark.parametrize("value", [True, False])
async def test_bool_is_not_taken_for_a_number(
    conn: fastpg.Connection,
    pg_type: str,
    value: bool,  # noqa: FBT001
) -> None:
    # bool is an int subclass in Python, but not a number to PostgreSQL.
    expected = rf"cannot encode bool as PostgreSQL type {pg_type}$"
    with pytest.raises(fastpg.ParameterError, match=expected):
        await conn.select_one(f"SELECT $1::{pg_type}", value)


async def test_naive_and_aware_datetimes(conn: fastpg.Connection) -> None:
    naive = dt.datetime(2024, 1, 1, 12)  # noqa: DTZ001 - naive on purpose
    aware = dt.datetime(2024, 1, 1, 12, tzinfo=dt.timezone(dt.timedelta(hours=-5)))
    with pytest.raises(fastpg.ParameterError, match="naive datetime passed for a timestamptz"):
        await conn.select_one("SELECT $1::timestamptz", naive)
    with pytest.raises(fastpg.ParameterError, match="aware datetime passed for a timestamp"):
        await conn.select_one("SELECT $1::timestamp", aware)
    utc = dt.datetime(2024, 1, 1, 17, tzinfo=dt.UTC)
    assert await conn.select_one("SELECT $1::timestamptz", aware) == (utc,)


async def test_none_compares_as_sql_null(conn: fastpg.Connection) -> None:
    await conn.execute_script(
        "CREATE TEMP TABLE maybe (value int); INSERT INTO maybe VALUES (NULL)"
    )
    # NULL = NULL is unknown, so `= $1` with None matches nothing, even the NULL row.
    assert await conn.select_many("SELECT value FROM maybe WHERE value = $1", None) == []
    null_safe = "SELECT value FROM maybe WHERE value IS NOT DISTINCT FROM $1"
    assert await conn.select_many(null_safe, None) == [(None,)]
    # Typed by the column, None goes anywhere.
    assert await conn.select_one("SELECT $1::uuid, $2::jsonb", None, None) == (None, None)


@pytest.mark.parametrize(
    ("query", "values", "expected", "got"),
    [
        ("SELECT $1::int, $2::int", (1,), 2, 1),
        ("SELECT $1::int", (1, 2), 1, 2),
        ("SELECT 1", (1,), 0, 1),
    ],
)
async def test_parameter_count_must_match(
    conn: fastpg.Connection,
    query: str,
    values: tuple[object, ...],
    expected: int,
    got: int,
) -> None:
    message = rf"^query expects {expected} parameter\(s\), got {got}$"
    with pytest.raises(fastpg.ParameterError, match=message):
        await conn.select_one(query, *values)
    with pytest.raises(fastpg.ParameterError, match=message):
        await conn.execute(query, *values)
    assert await conn.select_one("SELECT 1") == (1,)


@pytest.mark.parametrize(
    ("pg_type", "value"),
    [("int2", 2**15), ("int2", -(2**15) - 1), ("int4", 2**31), ("int4", -(2**31) - 1), ("oid", -1)],
)
async def test_int_out_of_range_for_its_type(
    conn: fastpg.Connection,
    pg_type: str,
    value: int,
) -> None:
    with pytest.raises(
        fastpg.ParameterError,
        match=f"int out of range for PostgreSQL type {pg_type}",
    ):
        await conn.select_one(f"SELECT $1::{pg_type}", value)


@pytest.mark.parametrize(
    ("pg_type", "value"),
    [
        ("int2", -(2**15)),
        ("int4", 2**31 - 1),
        ("int8", -(2**63)),
        ("int8", 2**63 - 1),
        ("oid", 2**32 - 1),
    ],
)
async def test_int_at_the_edge_of_its_type(
    conn: fastpg.Connection,
    pg_type: str,
    value: int,
) -> None:
    assert await conn.select_one(f"SELECT $1::{pg_type}", value) == (value,)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("NaN"), "NaN"),
        (Decimal("-NaN"), "NaN"),
        (Decimal("sNaN"), "NaN"),
        (Decimal("Infinity"), "Infinity"),
        (Decimal("-Infinity"), "-Infinity"),
        (Decimal("-0"), "0"),
        (Decimal("0.1000"), "0.1000"),
        (Decimal("1E+3"), "1000"),
        (Decimal("-1.5E-7"), "-0.00000015"),
    ],
)
async def test_decimal_special_values(
    conn: fastpg.Connection,
    value: Decimal,
    expected: str,
) -> None:
    assert await conn.select_one("SELECT $1::numeric::text", value) == (expected,)


@pytest.mark.parametrize(
    ("value", "error", "message"),
    [
        (Decimal("NaN123"), fastpg.ParameterError, 'invalid numeric value "NaN123"'),
        (Decimal("1E+200000"), fastpg.ParameterError, "is out of range"),
        (Decimal("1E-20000"), fastpg.DataError, 'invalid scale in external "numeric" value'),
    ],
)
async def test_decimal_numeric_cannot_hold(
    conn: fastpg.Connection,
    value: Decimal,
    error: type[fastpg.FastPGError],
    message: str,
) -> None:
    with pytest.raises(error, match=message):
        await conn.select_one("SELECT $1::numeric", value)
    assert await conn.select_one("SELECT 1") == (1,)


async def test_nul_in_a_text_parameter_is_refused_by_the_server(conn: fastpg.Connection) -> None:
    with pytest.raises(fastpg.DataError, match='invalid byte sequence for encoding "UTF8": 0x00'):
        await conn.select_one("SELECT $1::text", "a\x00b")
    with pytest.raises(fastpg.DataError, match=r"\\u0000 cannot be converted to text"):
        await conn.select_one("SELECT $1::jsonb", {"key": "a\x00b"})
    assert await conn.select_one("SELECT $1::json", {"key": "a\x00b"}) == ({"key": "a\x00b"},)
    assert await conn.select_one("SELECT $1::bytea", b"a\x00b") == (b"a\x00b",)


async def test_non_finite_float_in_a_json_parameter_becomes_null(conn: fastpg.Connection) -> None:
    # msgspec's JSON encoding of NaN and infinities (JSON has no form for them).
    assert await conn.select_one("SELECT $1::jsonb", [math.nan, math.inf]) == ([None, None],)
