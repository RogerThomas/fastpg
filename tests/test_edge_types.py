"""Types fastpg doesn't decode, and user-defined types built on ones it does."""

from typing import Any, NamedTuple, TypeIs

import msgspec
import pydantic
import pytest

import fastpg
from fastpg.errors import CheckViolationError


class AnyValue(msgspec.Struct):
    value: Any


class AnyValueTuple(NamedTuple):
    value: Any


class AnyValueModel(pydantic.BaseModel):
    value: Any


class Number(msgspec.Struct):
    number: int


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
    name="target",
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
def _target(request: pytest.FixtureRequest) -> Target:
    """Every target and strategy, with a field taking whatever the column holds."""
    return request.param


@pytest.mark.parametrize(
    ("expression", "type_name"),
    [
        ("ARRAY[1, 2]", "_int4"),
        ("ARRAY['a']", "_text"),
        ("'1 day'::interval", "interval"),
        ("int4range(1, 5)", "int4range"),
        ("tstzrange('2024-01-01', '2024-01-02')", "tstzrange"),
        ("ROW(1, 'a')", "record"),
        ("'1.2.3.4'::inet", "inet"),
        ("'10.0.0.0/8'::cidr", "cidr"),
        ("'1.50'::money", "money"),
        ("'<a/>'::xml", "xml"),
        ("B'101'", "bit"),
        ("B'101'::varbit", "varbit"),
        ("'12:00+02'::timetz", "timetz"),
        ("'08:00:2b:01:02:03'::macaddr", "macaddr"),
        ("point(1, 2)", "point"),
        ("'a b'::tsvector", "tsvector"),
        ("'pg_class'::regclass", "regclass"),
    ],
)
async def test_unsupported_type_names_the_column_and_type(
    conn: fastpg.Connection,
    target: Target,
    expression: str,
    type_name: str,
) -> None:
    into, strategy = target
    query = f"SELECT {expression} AS value FROM generate_series(1, 1000)"  # noqa: S608
    expected = rf'^column "value" has PostgreSQL type {type_name}, which fastpg cannot decode$'
    with pytest.raises(fastpg.UnsupportedTypeError, match=expected):
        await conn.select_many(query, into=into, strategy=strategy)
    with pytest.raises(fastpg.UnsupportedTypeError, match=expected):  # even with no rows
        await conn.select_many(f"{query} WHERE false", into=into, strategy=strategy)
    with pytest.raises(fastpg.UnsupportedTypeError, match=expected):  # tuples too
        await conn.select_one_or_none(f"{query} LIMIT 1")
    assert await conn.select_one("SELECT 1") == (1,)
    assert await conn.select_many(f"SELECT ({expression})::text") != []  # cast it in SQL


async def test_composite_type(conn: fastpg.Connection, target: Target) -> None:
    await conn.execute_script("CREATE TYPE pg_temp.pair AS (x int, y text)")
    into, strategy = target
    expected = r'^column "value" has PostgreSQL type pair, which fastpg cannot decode$'
    with pytest.raises(fastpg.UnsupportedTypeError, match=expected):
        await conn.select_one(
            "SELECT ROW(1, 'a')::pg_temp.pair AS value",
            into=into,
            strategy=strategy,
        )


async def test_hstore(conn: fastpg.Connection) -> None:
    try:
        await conn.execute_script("CREATE EXTENSION IF NOT EXISTS hstore")
    except fastpg.DatabaseError as e:  # not installed, or not allowed on a FASTPG_DSN server
        pytest.skip(f"hstore unavailable: {e}")
    try:
        with pytest.raises(fastpg.UnsupportedTypeError, match="PostgreSQL type hstore"):
            await conn.select_one("SELECT 'a=>1'::hstore")
        with pytest.raises(
            fastpg.ParameterError,
            match="cannot encode str as PostgreSQL type hstore",
        ):
            await conn.select_one("SELECT $1::hstore", "a=>1")
        assert await conn.select_one("SELECT $1::text::hstore -> 'a'", "a=>1") == ("1",)
    finally:
        await conn.execute_script("DROP EXTENSION hstore")


async def test_unsupported_column_no_field_reads_is_ignored_when_built_directly(
    conn: fastpg.Connection,
) -> None:
    query = "SELECT 1 AS number, '1 day'::interval AS extra"
    assert await conn.select_one(query, into=Number) == Number(1)


@pytest.mark.xfail(
    strict=True,
    raises=fastpg.UnsupportedTypeError,
    reason="open: the msgpack/json routes encode every column, read by a field or not"
    " (notes/edge-cases-data.md)",
)
@pytest.mark.parametrize("strategy", ["msgpack", "json"])
async def test_unsupported_column_no_field_reads_is_ignored_through_validation(
    conn: fastpg.Connection,
    strategy: fastpg.Strategy,
) -> None:
    query = "SELECT 1 AS number, '1 day'::interval AS extra"
    assert await conn.select_one(query, into=Number, strategy=strategy) == Number(1)


@pytest.mark.parametrize(
    ("type_name", "value", "text"),
    [
        ("int4[]", [1, 2], "{1,2}"),
        ("interval", "1 day", "1 day"),
        ("inet", "1.2.3.4/32", "1.2.3.4/32"),
        ("xml", "<a/>", "<a/>"),
        ("money", "1.50", "$1.50"),
        ("int4range", "[1,5)", "[1,5)"),
        ("varbit", "101", "101"),
    ],
)
async def test_unsupported_parameter_type_names_it(
    conn: fastpg.Connection,
    type_name: str,
    value: object,
    text: str,
) -> None:
    internal = f"_{type_name[:-2]}" if type_name.endswith("[]") else type_name
    expected = rf"^parameter \$1: cannot encode .* as PostgreSQL type {internal}$"
    with pytest.raises(fastpg.ParameterError, match=expected):
        await conn.select_one(f"SELECT $1::{type_name}::text", value)
    # The workaround: send the value's text and cast it in SQL.
    assert await conn.select_one(f"SELECT $1::text::{type_name}::text", text) == (text,)


async def test_domains_decode_as_their_base_type(conn: fastpg.Connection, target: Target) -> None:
    await conn.execute_script(
        "CREATE DOMAIN pg_temp.positive AS int4 CHECK (VALUE > 0);"
        "CREATE DOMAIN pg_temp.short AS text CHECK (length(VALUE) < 8)",
    )
    into, strategy = target
    for expression, expected in (("5::pg_temp.positive", 5), ("'label'::pg_temp.short", "label")):
        row = await conn.select_one(f"SELECT {expression} AS value", into=into, strategy=strategy)
        assert _value_of(row) == expected


async def test_domain_parameters_are_encoded_as_their_base_type(conn: fastpg.Connection) -> None:
    await conn.execute_script(
        "CREATE DOMAIN pg_temp.positive AS int4 CHECK (VALUE > 0);"
        "CREATE DOMAIN pg_temp.short AS text CHECK (length(VALUE) < 8);"
        "CREATE TEMP TABLE domains (number pg_temp.positive, label pg_temp.short)",
    )
    assert await conn.execute("INSERT INTO domains VALUES ($1, $2)", 5, "label") == 1
    assert await conn.select_one("SELECT $1::pg_temp.positive", 6) == (6,)
    assert await conn.select_many("SELECT number, label FROM domains") == [(5, "label")]


async def test_domain_parameter_of_the_wrong_type_names_both_types(
    conn: fastpg.Connection,
) -> None:
    await conn.execute_script("CREATE DOMAIN pg_temp.positive AS int4 CHECK (VALUE > 0)")
    expected = r"cannot encode str as PostgreSQL type positive \(a domain over int4\)"
    with pytest.raises(fastpg.ParameterError, match=expected):
        await conn.select_one("SELECT $1::pg_temp.positive", "five")


async def test_domain_check_violation_is_an_integrity_error(conn: fastpg.Connection) -> None:
    await conn.execute_script("CREATE DOMAIN pg_temp.positive AS int4 CHECK (VALUE > 0)")
    with pytest.raises(CheckViolationError):
        await conn.select_one("SELECT $1::pg_temp.positive", -1)
    assert await conn.select_one("SELECT 1") == (1,)


async def test_enum_label_added_while_statements_are_cached(
    conn: fastpg.Connection,
    target: Target,
) -> None:
    into, strategy = target
    await conn.execute_script(
        "CREATE TYPE pg_temp.mood AS ENUM ('happy', 'sad');"
        "CREATE TEMP TABLE moods (value pg_temp.mood); INSERT INTO moods VALUES ('happy')",
    )
    query = "SELECT value FROM moods ORDER BY value"
    insert = "INSERT INTO moods VALUES ($1)"
    rows = await conn.select_many(query, into=into, strategy=strategy)
    assert [_value_of(row) for row in rows] == ["happy"]
    assert await conn.execute(insert, "sad") == 1
    await conn.execute_script("ALTER TYPE pg_temp.mood ADD VALUE 'calm' BEFORE 'happy'")
    assert await conn.execute(insert, "calm") == 1
    rows = await conn.select_many(query, into=into, strategy=strategy)
    assert [_value_of(row) for row in rows] == ["calm", "happy", "sad"]
    with pytest.raises(fastpg.DataError, match='invalid input value for enum mood: "angry"'):
        await conn.execute(insert, "angry")
