"""Values PostgreSQL holds that Python can't represent, or only approximately."""

import datetime as dt
import math
import secrets
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Annotated, Any, NamedTuple, TypeIs
from urllib.parse import urlsplit

import msgspec
import pydantic
import pytest

import fastpg


class AnyValue(msgspec.Struct):
    value: Any


class AnyValueTuple(NamedTuple):
    value: Any


class AnyValueModel(pydantic.BaseModel):
    value: Any


class TextValue(msgspec.Struct):
    value: str


class TextValueTuple(NamedTuple):
    value: str


class TextValueModel(pydantic.BaseModel):
    value: str


class IntValue(msgspec.Struct):
    value: int


class IntValueModel(pydantic.BaseModel):
    value: int


class FloatValue(msgspec.Struct):
    value: float


class FloatValueModel(pydantic.BaseModel):
    value: float


class DecimalValue(msgspec.Struct):
    value: Decimal


class DecimalValueModel(pydantic.BaseModel):
    value: Decimal


class NonFiniteDecimalValueModel(pydantic.BaseModel):
    value: Annotated[Decimal, pydantic.Field(allow_inf_nan=True)]


class TimeValue(msgspec.Struct):
    value: dt.time


class TimeValueTuple(NamedTuple):
    value: dt.time


class TimeValueModel(pydantic.BaseModel):
    value: dt.time


class DateTimeValue(msgspec.Struct):
    value: dt.datetime


class DateTimeValueModel(pydantic.BaseModel):
    value: dt.datetime


type Target = tuple[type[object], fastpg.Strategy]


def _target_id(target: Target) -> str:
    into, strategy = target
    return f"{into.__name__}-{strategy}"


def _is_dict_row(row: object) -> TypeIs[dict[str, object]]:
    return isinstance(row, dict)


def _value_of(row: object) -> object:
    """The `value` column of a row of any target."""
    if _is_dict_row(row):
        return row["value"]
    return getattr(row, "value")  # noqa: B009 - a Struct, NamedTuple or model


@pytest.fixture(
    name="any_target",
    params=[
        (dict, "auto"),
        (AnyValue, "direct"),
        (AnyValue, "msgpack"),
        (AnyValue, "json"),
        (AnyValueTuple, "auto"),
        (AnyValueModel, "direct"),
        (AnyValueModel, "json"),
    ],
    ids=_target_id,
)
def _any_target(request: pytest.FixtureRequest) -> Target:
    """Every target and strategy, with a field taking whatever the column holds."""
    return request.param


@pytest.fixture(
    name="text_target",
    params=[
        (dict, "auto"),
        (TextValue, "direct"),
        (TextValue, "msgpack"),
        (TextValue, "json"),
        (TextValueTuple, "auto"),
        (TextValueModel, "direct"),
        (TextValueModel, "json"),
    ],
    ids=_target_id,
)
def _text_target(request: pytest.FixtureRequest) -> Target:
    """Every target and strategy, with a `str` field."""
    return request.param


@pytest.fixture(name="sql_ascii_dsn", scope="module")
async def _sql_ascii_dsn(dsn: str) -> AsyncIterator[str]:
    """A throwaway database with encoding SQL_ASCII, which stores text bytes unchecked."""
    name = f"fastpg_sql_ascii_{secrets.token_hex(4)}"
    create = f"CREATE DATABASE {name} ENCODING 'SQL_ASCII' LC_COLLATE 'C' LC_CTYPE 'C'"
    async with await fastpg.connect(dsn) as conn:
        try:
            await conn.execute_script(f"{create} TEMPLATE template0")
        except fastpg.ProgrammingError as e:  # no CREATEDB privilege on a FASTPG_DSN server
            pytest.skip(f"cannot create an SQL_ASCII database: {e}")
    try:
        yield urlsplit(dsn)._replace(path=f"/{name}").geturl()
    finally:
        async with await fastpg.connect(dsn) as conn:
            await conn.execute_script(f"DROP DATABASE {name} WITH (FORCE)")


# --- dates and times -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pg_type", "literal"),
    [
        ("date", "infinity"),
        ("date", "-infinity"),
        ("date", "10000-01-01"),
        ("date", "0044-03-15 BC"),
        ("timestamp", "infinity"),
        ("timestamp", "-infinity"),
        ("timestamp", "10000-01-01 00:00"),
        ("timestamp", "0001-12-31 23:59:59 BC"),
        ("timestamptz", "infinity"),
        ("timestamptz", "-infinity"),
        ("timestamptz", "10000-01-01 00:00+00"),
        ("timestamptz", "0001-12-31 23:59:59+00 BC"),
    ],
)
async def test_date_python_cannot_represent_is_a_decode_error(
    conn: fastpg.Connection,
    any_target: Target,
    pg_type: str,
    literal: str,
) -> None:
    into, strategy = any_target
    query = f"SELECT '{literal}'::{pg_type} AS value"
    expected = r'^column "value".*: (infinite .* cannot be represented|year -?\d+ is out of range)'
    with pytest.raises(fastpg.DecodeError, match=expected):
        await conn.select_one(query, into=into, strategy=strategy)
    with pytest.raises(fastpg.DecodeError, match=expected):
        await conn.select_one(query)
    assert await conn.select_one("SELECT 1") == (1,)


@pytest.mark.parametrize(
    ("pg_type", "literal", "expected"),
    [
        ("date", "0001-01-01", dt.date(1, 1, 1)),
        ("date", "9999-12-31", dt.date(9999, 12, 31)),
        ("timestamp", "0001-01-01 00:00", dt.datetime(1, 1, 1)),  # noqa: DTZ001 - naive
        (
            "timestamptz",
            "9999-12-31 23:59:59.999999+00",
            dt.datetime(9999, 12, 31, 23, 59, 59, 999_999, tzinfo=dt.UTC),
        ),
    ],
)
async def test_first_and_last_dates_python_represents(
    conn: fastpg.Connection,
    any_target: Target,
    pg_type: str,
    literal: str,
    expected: object,
) -> None:
    into, strategy = any_target
    query = f"SELECT '{literal}'::{pg_type} AS value"
    row = await conn.select_one(query, into=into, strategy=strategy)
    value = _value_of(row)
    # msgpack/json give an Any field the ISO text instead (see notes/edge-cases-data.md).
    assert value == expected or value == str(expected).replace(" ", "T").replace("+00:00", "Z")


@pytest.mark.parametrize(
    ("into", "strategy"),
    [
        (dict, "auto"),
        (TimeValue, "direct"),
        (TimeValue, "msgpack"),
        (TimeValue, "json"),
        (TimeValueTuple, "auto"),
        (TimeValueModel, "direct"),
        (TimeValueModel, "json"),
    ],
)
async def test_time_2400_is_a_decode_error(
    conn: fastpg.Connection,
    into: type[object],
    strategy: fastpg.Strategy,
) -> None:
    query = "SELECT '24:00:00'::time AS value"
    with pytest.raises(fastpg.DecodeError, match="value"):
        await conn.select_one(query, into=into, strategy=strategy)
    with pytest.raises(fastpg.DecodeError, match=r'column "value" \(time\)') as exc:
        await conn.select_one(query)
    assert isinstance(exc.value.__cause__, ValueError)  # hour must be in 0..23


@pytest.mark.parametrize(
    "time_zone",
    [
        "UTC",
        "Asia/Kathmandu",  # +05:45
        "America/St_Johns",  # -02:30
        "Pacific/Kiritimati",  # +14
        "Etc/GMT+12",  # -12 (POSIX sign)
        "<+0330>-03:30",  # a POSIX zone spec
    ],
)
@pytest.mark.parametrize(
    ("into", "strategy"),
    [
        (DateTimeValue, "direct"),
        (DateTimeValue, "msgpack"),
        (DateTimeValue, "json"),
        (DateTimeValueModel, "direct"),
        (DateTimeValueModel, "json"),
    ],
)
async def test_timestamptz_is_the_same_instant_whatever_the_session_time_zone(
    conn: fastpg.Connection,
    time_zone: str,
    into: type[DateTimeValue | DateTimeValueModel],
    strategy: fastpg.Strategy,
) -> None:
    await conn.execute_script(f"SET TimeZone = '{time_zone}'")
    instant = dt.datetime(2024, 6, 1, 12, tzinfo=dt.UTC)
    query = "SELECT '2024-06-01 12:00:00+00'::timestamptz AS value"
    row = await conn.select_one(query, into=into, strategy=strategy)
    assert row.value == instant
    assert row.value.utcoffset() == dt.timedelta(0)
    assert await conn.select_one("SELECT $1::timestamptz", instant) == (instant,)
    (as_text,) = await conn.select_one("SELECT $1::timestamptz::text", instant)
    assert as_text != "2024-06-01 12:00:00+00" or time_zone == "UTC"  # the zone did apply


# --- numbers ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "1" + "0" * 1000 + "." + "9" * 1000,
        "0." + "0" * 16382 + "1",  # numeric's largest scale
        "-" + "9" * 131072,  # its most digits before the point
    ],
    ids=["1000.1000-digits", "scale-16383", "131072-digits"],
)
@pytest.mark.parametrize(
    ("into", "strategy"),
    [
        (DecimalValue, "direct"),
        (DecimalValue, "msgpack"),
        (DecimalValue, "json"),
        (DecimalValueModel, "direct"),
        (DecimalValueModel, "json"),
    ],
)
async def test_numeric_digits_survive_exactly(
    conn: fastpg.Connection,
    text: str,
    into: type[DecimalValue | DecimalValueModel],
    strategy: fastpg.Strategy,
) -> None:
    query = "SELECT $1::text::numeric AS value"
    row = await conn.select_one(query, text, into=into, strategy=strategy)
    assert row.value.as_tuple() == Decimal(text).as_tuple()  # every digit, and the exponent
    (value,) = await conn.select_one("SELECT $1::text::numeric", text)
    assert value.as_tuple() == Decimal(text).as_tuple()
    assert await conn.select_one("SELECT $1::numeric::text", Decimal(text)) == (text,)


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
@pytest.mark.parametrize(
    ("into", "strategy"),
    [
        (DecimalValue, "direct"),
        (DecimalValue, "msgpack"),
        (DecimalValue, "json"),
        (NonFiniteDecimalValueModel, "auto"),
    ],
)
async def test_non_finite_numeric(
    conn: fastpg.Connection,
    literal: str,
    into: type[DecimalValue | NonFiniteDecimalValueModel],
    strategy: fastpg.Strategy,
) -> None:
    query = f"SELECT '{literal}'::numeric AS value"
    row = await conn.select_one(query, into=into, strategy=strategy)
    assert str(row.value) == literal


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
@pytest.mark.parametrize("strategy", ["direct", "json"])
async def test_non_finite_numeric_into_a_pydantic_decimal_is_refused(
    conn: fastpg.Connection,
    literal: str,
    strategy: fastpg.Strategy,
) -> None:
    # pydantic's Decimal must be finite unless allow_inf_nan=True, and direct construction
    # (what "auto" picks for this model) must agree with validation.
    query = f"SELECT '{literal}'::numeric AS value"
    with pytest.raises(fastpg.DecodeError, match="finite"):
        await conn.select_one(query, into=DecimalValueModel, strategy=strategy)
    assert await conn.select_one("SELECT 1") == (1,)


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
@pytest.mark.parametrize(
    ("into", "strategy"),
    [
        (FloatValue, "direct"),
        (FloatValue, "msgpack"),
        (FloatValueModel, "direct"),
        (FloatValueModel, "json"),
    ],
)
async def test_non_finite_float8(
    conn: fastpg.Connection,
    literal: str,
    into: type[FloatValue | FloatValueModel],
    strategy: fastpg.Strategy,
) -> None:
    query = f"SELECT '{literal}'::float8 AS value"
    row = await conn.select_one(query, into=into, strategy=strategy)
    assert math.isnan(row.value) if literal == "NaN" else row.value == float(literal)


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
async def test_non_finite_float8_has_no_json_form_for_msgspec(
    conn: fastpg.Connection,
    literal: str,
) -> None:
    query = f"SELECT '{literal}'::float8 AS value"
    expected = rf'^column "value": float8 {literal} has no JSON form msgspec accepts; use'
    with pytest.raises(fastpg.DecodeError, match=expected):
        await conn.select_one(query, into=FloatValue, strategy="json")
    assert await conn.select_one("SELECT 1") == (1,)


@pytest.mark.parametrize(
    ("into", "strategy"),
    [
        (FloatValue, "direct"),
        (FloatValue, "msgpack"),
        (FloatValue, "json"),
        (FloatValueModel, "direct"),
        (FloatValueModel, "json"),
    ],
)
async def test_negative_zero_float8_keeps_its_sign(
    conn: fastpg.Connection,
    into: type[FloatValue | FloatValueModel],
    strategy: fastpg.Strategy,
) -> None:
    row = await conn.select_one("SELECT '-0'::float8 AS value", into=into, strategy=strategy)
    assert math.copysign(1, row.value) == -1
    (value,) = await conn.select_one("SELECT '-0'::float8")
    assert math.copysign(1, value) == -1


@pytest.mark.parametrize("number", [-(2**63), 2**63 - 1])
@pytest.mark.parametrize(
    ("into", "strategy"),
    [
        (IntValue, "direct"),
        (IntValue, "msgpack"),
        (IntValue, "json"),
        (IntValueModel, "direct"),
        (IntValueModel, "json"),
        (FloatValue, "direct"),
        (FloatValue, "msgpack"),
        (FloatValue, "json"),
        (FloatValueModel, "direct"),
        (FloatValueModel, "json"),
    ],
)
async def test_int8_extremes(
    conn: fastpg.Connection,
    number: int,
    into: type[IntValue | IntValueModel | FloatValue | FloatValueModel],
    strategy: fastpg.Strategy,
) -> None:
    query = f"SELECT ({number})::int8 AS value"
    row = await conn.select_one(query, into=into, strategy=strategy)
    # A float field rounds, as Python's float(int) does: 2**63 - 1 becomes 2.0**63.
    expected = float(number) if into in (FloatValue, FloatValueModel) else number
    assert row.value == expected
    assert type(row.value) is type(expected)


# --- text ------------------------------------------------------------------------------


async def test_invalid_utf8_from_the_server_is_a_decode_error(
    conn: fastpg.Connection,
    text_target: Target,
) -> None:
    into, strategy = text_target
    # The server converts text to the client encoding: Latin-1 "é" is the byte 0xe9 alone,
    # not UTF-8.
    await conn.execute_script("SET client_encoding = 'LATIN1'")
    query = "SELECT chr(233) AS value"
    with pytest.raises(fastpg.DecodeError, match=r'column "value"'):
        await conn.select_one(query, into=into, strategy=strategy)
    with pytest.raises(fastpg.DecodeError, match=r'column "value" \(text\)') as exc:
        await conn.select_one(query)
    assert isinstance(exc.value.__cause__, UnicodeDecodeError)
    await conn.execute_script("RESET client_encoding")
    assert await conn.select_one(query) == ("é",)


@pytest.mark.xfail(
    strict=True,
    reason="open: a session whose client_encoding isn't UTF8 gets fastpg's UTF-8 query text"
    " and parameters reinterpreted, silently (notes/edge-cases-data.md)",
)
async def test_client_encoding_other_than_utf8_does_not_corrupt_parameters(
    conn: fastpg.Connection,
) -> None:
    await conn.execute_script("SET client_encoding = 'LATIN1'")
    assert await conn.select_one("SELECT length($1::text)", "é") == (1,)  # got (2,): "Ã©"


async def test_text_holding_nul_is_refused_by_the_server(conn: fastpg.Connection) -> None:
    with pytest.raises(fastpg.DatabaseError, match="null character not permitted"):
        await conn.select_one("SELECT chr(0)")
    with pytest.raises(fastpg.DataError, match="0x00"):
        await conn.select_one("SELECT $1::text", "a\x00b")
    assert await conn.select_one("SELECT 1") == (1,)


async def test_nul_only_reaches_python_through_json(conn: fastpg.Connection) -> None:
    assert await conn.select_one("""SELECT '"a\\u0000b"'::json""") == ("a\x00b",)
    with pytest.raises(fastpg.DataError, match=r"\\u0000 cannot be converted to text"):
        await conn.select_one("""SELECT '"a\\u0000b"'::jsonb""")


async def test_invalid_utf8_stored_in_an_sql_ascii_database(sql_ascii_dsn: str) -> None:
    query = "SELECT E'\\xff' || 'a' AS value"
    async with await fastpg.connect(sql_ascii_dsn) as conn:
        # Sent to a UTF8 client, the server checks the bytes and refuses them.
        with pytest.raises(fastpg.DataError, match='invalid byte sequence for encoding "UTF8"'):
            await conn.select_one(query)
        # Unless the client asks for SQL_ASCII too: then they arrive unchecked.
        await conn.execute_script("SET client_encoding = 'SQL_ASCII'")
        with pytest.raises(fastpg.DecodeError, match=r'column "value" \(text\)'):
            await conn.select_one(query)
        for strategy in ("msgpack", "json"):
            with pytest.raises(fastpg.DecodeError, match=r'column "value": invalid utf-8'):
                await conn.select_one(query, into=TextValue, strategy=strategy)
        # The bytes themselves are intact: as bytea, they decode.
        as_bytes = "SELECT convert_to(E'\\xff' || 'a', 'SQL_ASCII')"
        assert await conn.select_one(as_bytes) == (b"\xffa",)
