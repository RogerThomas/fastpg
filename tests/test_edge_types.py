"""Types fastpg doesn't decode, and user-defined types built on ones it does."""

import pytest

import fastpg
from fastpg.errors import CheckViolationError


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
