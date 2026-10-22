"""t-string (PEP 750) queries: interpolations are bound parameters, never SQL text."""

import asyncio
import datetime as dt
import enum
import uuid
from decimal import Decimal
from string.templatelib import Interpolation, Template
from typing import TYPE_CHECKING, Any, assert_type

import msgspec
import pytest

import fastpg
from fastpg import Identifier

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class Person(msgspec.Struct):
    name: str
    age: int


class Item(msgspec.Struct):
    id: int
    v: str


@pytest.fixture(name="pool")
async def _pool(dsn: str) -> AsyncIterator[fastpg.Pool]:
    async with await fastpg.create_pool(dsn, max_size=4) as pool:
        yield pool


class Color(enum.Enum):
    RED = "red"


async def _prepared_sql(conn: fastpg.Connection, marker: str) -> str:
    """The SQL text the server prepared for the statement containing `marker`."""
    (statement,) = await conn.select_one(
        "SELECT statement FROM pg_prepared_statements WHERE strpos(statement, $1) > 0",
        marker,
    )
    return statement


async def test_select_methods_accept_templates(conn: fastpg.Connection) -> None:
    person_id = 42
    person = await conn.select_one(
        t"SELECT name, age FROM people WHERE id = {person_id}",
        into=Person,
    )
    assert_type(person, Person)
    assert person == await conn.select_one(
        "SELECT name, age FROM people WHERE id = $1",
        42,
        into=Person,
    )
    missing = await conn.select_one_or_none(
        t"SELECT name, age FROM people WHERE id = {-1}",
        into=Person,
    )
    assert_type(missing, Person | None)
    assert missing is None
    people = await conn.select_many(
        t"SELECT name, age FROM people WHERE id <= {3} ORDER BY id",
        into=Person,
    )
    assert_type(people, list[Person])
    assert [p.name for p in people] == ["person-1", "person-2", "person-3"]
    row = await conn.select_one(t"SELECT {1}::int + {2}::int, 'no-interpolations'")
    assert_type(row, tuple[Any, ...])
    assert row == (3, "no-interpolations")
    assert await conn.select_one(t"SELECT 1") == (1,)


async def test_execute_with_template(conn: fastpg.Connection) -> None:
    await conn.execute_script(
        "DROP TABLE IF EXISTS tstr_exec; CREATE TABLE tstr_exec (id int PRIMARY KEY, v text)",
    )
    assert await conn.execute(t"INSERT INTO tstr_exec VALUES ({1}, {'a'}), ({2}, {None})") == 2
    value, row_id = "b", 2
    assert await conn.execute(t"UPDATE tstr_exec SET v = {value} WHERE id = {row_id}") == 1
    assert await conn.select_many(t"SELECT * FROM tstr_exec ORDER BY id") == [(1, "a"), (2, "b")]
    await conn.execute_script("DROP TABLE tstr_exec")


async def test_generated_sql_numbers_placeholders(conn: fastpg.Connection) -> None:
    a, b = 1, "b"
    assert await conn.select_one(t"SELECT {a}::int AS tstr_numbering, {b}::text") == (1, "b")
    assert (
        await _prepared_sql(conn, "tstr_numbering") == "SELECT $1::int AS tstr_numbering, $2::text"
    )


async def test_template_shares_prepared_statement_with_equivalent_sql(
    conn: fastpg.Connection,
) -> None:
    assert await conn.select_one("SELECT $1::int + $2::int", 1, 2) == (3,)
    before = conn.cached_statements
    a, b = 3, 4
    assert await conn.select_one(t"SELECT {a}::int + {b}::int") == (7,)
    where = t"{a}::int + {b}::int"
    assert await conn.select_one(t"SELECT {where}") == (7,)
    assert conn.cached_statements == before


@pytest.mark.parametrize(
    "value",
    [
        "'",
        "''; DROP TABLE people; --",
        "' OR '1'='1",
        "-- comment",
        "/* comment */",
        "$1",
        "$1::int",
        "$$",
        '"',
        "\\",
        ";",
        "é ✓ 𝄞",
    ],
)
async def test_values_are_data_not_sql(conn: fastpg.Connection, value: str) -> None:
    assert await conn.select_one(t"SELECT {value}::text") == (value,)
    assert await conn.select_one(t"SELECT count(*) FROM people WHERE name = {value}") == (0,)
    assert await conn.select_one(t"SELECT count(*) FROM people") == (100_000,)


async def test_values_survive_a_table_round_trip(conn: fastpg.Connection) -> None:
    await conn.execute_script("DROP TABLE IF EXISTS tstr_data; CREATE TABLE tstr_data (v text)")
    value = "x'); DELETE FROM tstr_data; --"
    await conn.execute(t"INSERT INTO tstr_data VALUES ({value})")
    await conn.execute(t"INSERT INTO tstr_data VALUES ({value})")
    assert await conn.select_many(t"SELECT v FROM tstr_data WHERE v = {value}") == [(value,)] * 2
    await conn.execute_script("DROP TABLE tstr_data")


async def test_casts_and_nulls(conn: fastpg.Connection) -> None:
    text, missing = "7", None
    assert await conn.select_one(t"SELECT {text}::text::int + 1") == (8,)
    assert await conn.select_one(t"SELECT {missing}::int, {missing}::text, {None}::jsonb") == (
        None,
        None,
        None,
    )
    assert await conn.select_one(
        t"SELECT count(*) FROM people WHERE id <= 14 AND email IS NOT DISTINCT FROM {missing}",
    ) == (2,)


@pytest.mark.parametrize(
    ("pg_type", "value", "expected"),
    [
        ("bool", True, True),
        ("int2", 1, 1),
        ("int4", 1, 1),
        ("int8", 1, 1),
        ("float4", 1.5, 1.5),
        ("float8", 1.5, 1.5),
        ("numeric", Decimal("1.5"), Decimal("1.5")),
        ("numeric", 1, Decimal(1)),
        ("text", "text", "text"),
        ("varchar", "varchar", "varchar"),
        ("text", Color.RED, "red"),
        ("bytea", b"bytea", b"bytea"),
        ("bytea", bytearray(b"bytea"), b"bytea"),
        ("bytea", memoryview(b"bytea"), b"bytea"),
        ("uuid", uuid.UUID(int=1), uuid.UUID(int=1)),
        ("date", dt.date(2000, 1, 1), dt.date(2000, 1, 1)),
        ("time", dt.time(1, 2, 3), dt.time(1, 2, 3)),
        ("timestamp", dt.datetime(2000, 1, 1), dt.datetime(2000, 1, 1)),  # noqa: DTZ001 - naive on purpose
        (
            "timestamptz",
            dt.datetime(2000, 1, 1, tzinfo=dt.UTC),
            dt.datetime(2000, 1, 1, tzinfo=dt.UTC),
        ),
        ("jsonb", {"key": "value"}, {"key": "value"}),
        ("json", ["json"], ["json"]),
    ],
)
async def test_every_param_type(
    conn: fastpg.Connection,
    pg_type: str,
    value: object,
    expected: object,
) -> None:
    # A type name is an identifier, so Identifier can splice it: SELECT $1::"int4".
    (got,) = await conn.select_one(t"SELECT {value}::{Identifier(pg_type)}")
    assert got == expected
    assert type(got) is type(expected)


async def test_lists_are_json_not_arrays(conn: fastpg.Connection) -> None:
    ids = [1, 2, 3]
    with pytest.raises(fastpg.ParameterError, match=r"dict/list \(JSON\) as PostgreSQL type _int4"):
        await conn.select_many(t"SELECT id FROM people WHERE id = ANY({ids})")
    # Until arrays are supported, unpack a JSON array server-side.
    rows = await conn.select_many(
        t"SELECT id FROM people WHERE id IN (SELECT jsonb_array_elements_text({ids})::int)"
        t" ORDER BY id",
    )
    assert rows == [(1,), (2,), (3,)]


async def test_fragments_compose_and_renumber(conn: fastpg.Connection) -> None:
    low, high, limit = 10, 20, 3
    between = t"id BETWEEN {low} AND {high}"
    where = t"{between} AND age >= {0}"
    rows = await conn.select_many(
        t"SELECT id AS tstr_compose FROM people WHERE {where} ORDER BY id LIMIT {limit}",
    )
    assert rows == [(10,), (11,), (12,)]
    assert await _prepared_sql(conn, "tstr_compose") == (
        "SELECT id AS tstr_compose FROM people"
        " WHERE id BETWEEN $1 AND $2 AND age >= $3 ORDER BY id LIMIT $4"
    )


async def test_cache_keys_on_spliced_text(conn: fastpg.Connection) -> None:
    # One outer literal, so one `strings` tuple: the spliced parts must key the cache.
    for fragment, expected in [
        (t"id = {1}", 1),
        (t"id IN ({1}, {2})", 2),
        (t"id < {4}", 3),
        (t"true", 100_000),
    ]:
        assert await conn.select_one(t"SELECT count(*) FROM people WHERE {fragment}") == (expected,)
    for value, expected in [(5, 5), (t"6", 6), (7, 7), (t"{8} + 1", 9)]:
        assert await conn.select_one(t"SELECT {value}::int") == (expected,)
    for column, expected in [("id", 1), ("name", "person-1")]:
        assert await conn.select_one(t"SELECT {Identifier(column)} FROM people WHERE id = 1") == (
            expected,
        )


async def test_concurrent_template_queries(conn: fastpg.Connection) -> None:
    ids = list(range(1, 201))
    results = await asyncio.gather(
        *(conn.select_one(t"SELECT id, name FROM people WHERE id = {i}") for i in ids),
    )
    assert results == [(i, f"person-{i}") for i in ids]


async def test_extra_params_with_template_rejected(conn: fastpg.Connection) -> None:
    with pytest.raises(TypeError, match="1 extra positional parameter"):
        await conn.select_one(t"SELECT {1}::int", 2)
    with pytest.raises(TypeError, match="2 extra positional parameter"):
        await conn.execute(t"SELECT {1}::int", 2, 3)


async def test_conversions_and_format_specs_rejected(conn: fastpg.Connection) -> None:
    value = 1
    # t"{value}" and t"{value!r}" share one `strings` tuple: a cached SQL text must not let
    # the conversion through.
    assert await conn.select_one(t"SELECT {value}::int") == (1,)
    with pytest.raises(ValueError, match=r"\{value!r\} has a conversion or format spec"):
        await conn.select_one(t"SELECT {value!r}::int")
    for template in [
        t"SELECT {value!s}",
        t"SELECT {value!a}",
        t"SELECT {value:d}",
        t"SELECT {value=}",
        t"SELECT {t'{value!r}'}",
    ]:
        with pytest.raises(ValueError, match="conversion or format spec"):
            await conn.select_one(template)


async def test_literal_placeholders_rejected(conn: fastpg.Connection) -> None:
    value = 1
    with pytest.raises(ValueError, match=r"literal \$n placeholder"):
        await conn.select_one(t"SELECT $1::int, {value}::int")
    with pytest.raises(ValueError, match=r"literal \$n placeholder"):
        await conn.select_one(t"SELECT {t'$2'}")
    # Conservative: also inside string literals. Send such text as a value instead.
    with pytest.raises(ValueError, match=r"literal \$n placeholder"):
        await conn.select_one(t"SELECT 'costs $5'")
    price = "costs $5"
    assert await conn.select_one(t"SELECT {price}::text") == ("costs $5",)
    # Dollar quotes ($$, $tag$) aren't placeholders.
    assert await conn.select_one(t"SELECT $$text$$, $tag$text$tag$") == ("text", "text")


async def test_placeholder_joins_rejected(conn: fastpg.Connection) -> None:
    value = 1
    with pytest.raises(ValueError, match=r"ends with '\$' right before an interpolation"):
        await conn.select_one(t"SELECT ${value}")
    with pytest.raises(ValueError, match="starts with a digit right after an interpolation"):
        await conn.select_one(t"SELECT {value}0")
    with pytest.raises(ValueError, match="starts with a digit right after a parameter"):
        await conn.select_one(t"SELECT {value}{t'0'}")
    with pytest.raises(ValueError, match="starts with a digit right after a parameter or '\\$'"):
        await conn.select_one(t"SELECT {t'$'}{t'1'}")
    with pytest.raises(ValueError, match="parameter is preceded directly by 'SELECT \\$'"):
        await conn.select_one(t"SELECT {t'$'}{value}")
    assert await conn.select_one(t"SELECT {t'1 + '}{value}") == (2,)


async def test_identifiers_are_quoted(conn: fastpg.Connection) -> None:
    table = Identifier('tstr "odd"; table')
    column = Identifier("Mixed Case")
    await conn.execute_script(
        t"DROP TABLE IF EXISTS {table}; CREATE TABLE {table} ({column} int)",
    )
    assert await conn.execute(t"INSERT INTO {table} ({column}) VALUES ({1})") == 1
    assert await conn.select_many(t"SELECT {column} FROM {table}") == [(1,)]
    assert await conn.select_one(t"SELECT relname FROM pg_class WHERE relname LIKE 'tstr %'") == (
        'tstr "odd"; table',
    )
    assert await conn.select_one(
        t"SELECT count(*) FROM {Identifier('public', 'people')} WHERE id = {1}",
    ) == (1,)
    await conn.execute_script(t"DROP TABLE {table}")


def test_identifier_value_semantics() -> None:
    assert Identifier("a", 'b"c').quoted == '"a"."b""c"'
    assert Identifier("a", "b").parts == ("a", "b")
    assert repr(Identifier("a", "b")) == "Identifier('a', 'b')"
    assert Identifier("a") == Identifier("a")
    assert Identifier("a") != Identifier("A")
    assert len({Identifier("a"), Identifier("a")}) == 1


def test_invalid_identifiers_rejected() -> None:
    with pytest.raises(ValueError, match="at least one name"):
        Identifier()
    with pytest.raises(ValueError, match="non-empty"):
        Identifier("")
    with pytest.raises(ValueError, match="no NUL"):
        Identifier("a\0b")
    untyped_caller: Any = 1  # what an untyped caller could pass
    with pytest.raises(TypeError, match="must be str, got int"):
        Identifier(untyped_caller)


async def test_glued_identifiers_rejected(conn: fastpg.Connection) -> None:
    name = Identifier("name")
    value = 1
    for template in [
        t"SELECT '{name}'",
        t'SELECT "{name}"',
        t"SELECT x_{name}",
        t"SELECT {name}_x",
        t"SELECT {name}{name}",
        t"SELECT {value}{t'{name}'}",
    ]:
        with pytest.raises(ValueError, match="must stand alone as a name"):
            await conn.select_one(template)
    with pytest.raises(ValueError, match="parameter is preceded directly by an Identifier"):
        await conn.select_one(t"SELECT {name}{value}")
    assert await conn.select_one(t"SELECT {name}, ({name}) FROM people WHERE id = 1") == (
        "person-1",
        "person-1",
    )


async def test_execute_script_takes_parameter_free_templates(conn: fastpg.Connection) -> None:
    table = Identifier("tstr_script")
    fill = t"INSERT INTO {table} VALUES (1); INSERT INTO {table} VALUES (2)"
    await conn.execute_script(
        t"DROP TABLE IF EXISTS {table}; CREATE TABLE {table} (id int); {fill}",
    )
    assert await conn.select_many(t"SELECT id FROM {table} ORDER BY id") == [(1,), (2,)]
    value = 3
    with pytest.raises(TypeError, match="simple query protocol, which has no parameters"):
        await conn.execute_script(t"INSERT INTO {table} VALUES ({value})")
    await conn.execute_script(t"DROP TABLE {table}")


async def test_many_dynamic_templates_stay_correct(conn: fastpg.Connection) -> None:
    # More distinct runtime-built templates than the SQL cache (1024) and the statement
    # cache (256) hold: eviction must never change results.
    for i in range(1100):
        template = Template(f"SELECT {i}::int4, ", Interpolation(i, "i"), "::int4")
        assert await conn.select_one(template) == (i, i)


async def test_params_with_a_t_string_is_refused(conn: fastpg.Connection) -> None:
    value = 1
    with pytest.raises(TypeError, match="a t-string takes its values from its interpolations"):
        await conn.select_one(t"SELECT {value}::int", params={"value": 1})
    with pytest.raises(TypeError, match="a t-string takes its values from its interpolations"):
        await conn.execute(t"SELECT {value}::int", params={"value": 1})
    assert await conn.select_one("SELECT 42") == (42,)


async def test_execute_script_refuses_a_template_with_values(conn: fastpg.Connection) -> None:
    value = 1
    with pytest.raises(TypeError, match="simple query protocol, which has no parameters"):
        await conn.execute_script(t"SELECT {value}")
    assert await conn.select_one("SELECT 42") == (42,)


async def test_pool_runs_t_string_queries(pool: fastpg.Pool) -> None:
    item_id = 7
    got = await pool.select_one(t"SELECT {item_id}::int4 AS id, {'v'}::text AS v", into=Item)
    assert got == Item(7, "v")
    assert await pool.select_many(t"SELECT generate_series(1, {3}::int4)") == [(1,), (2,), (3,)]


async def test_pool_t_string_script_with_identifier(pool: fastpg.Pool) -> None:
    table = Identifier("tstr_pool_items")
    await pool.execute_script(
        t"DROP TABLE IF EXISTS {table}; CREATE TABLE {table} (id int PRIMARY KEY, v text)",
    )
    await pool.execute_script(t"INSERT INTO {table} VALUES (1, 'v')")
    assert await pool.select_one(t"SELECT count(*) FROM {table}") == (1,)
    await pool.execute_script(t"DROP TABLE {table}")


async def test_template_and_named_params_rejected(conn: fastpg.Connection) -> None:
    a = 1
    with pytest.raises(TypeError, match="t-string"):
        await conn.select_one(t"SELECT {a}::int", params={"a": 1})
    with pytest.raises(TypeError, match="t-string"):
        await conn.execute(t"SELECT {a}::int", params={"a": 1})


async def test_pool_template_and_named_params_rejected(pool: fastpg.Pool) -> None:
    a = 1
    with pytest.raises(TypeError, match="t-string"):
        await pool.execute(t"SELECT {a}::int", params={"a": 1})
