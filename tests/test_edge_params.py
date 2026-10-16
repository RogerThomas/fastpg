"""Parameters that don't fit the type the server inferred for them."""

from decimal import Decimal

import pytest

import fastpg


class Opaque:
    """Not serialisable as JSON."""


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
