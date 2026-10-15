"""Named ``:name`` placeholders with values from ``params=``."""

import dataclasses
import types
from typing import Any, assert_type

import msgspec
import pytest
from pydantic import BaseModel

import fastpg


class Person(msgspec.Struct):
    name: str
    age: int


class PersonSelect(msgspec.Struct):
    id: int
    min_age: int


class RenamedSelect(msgspec.Struct, rename="camel"):
    id: int
    min_age: int


@dataclasses.dataclass
class DataclassSelect:
    id: int
    min_age: int


@dataclasses.dataclass(slots=True, frozen=True)
class SlotsSelect:
    id: int
    min_age: int


class ModelSelect(BaseModel):
    id: int
    min_age: int


class Item(msgspec.Struct):
    id: int
    v: str | None


class ItemModel(BaseModel):
    id: int
    v: str | None


async def _prepared_sql(conn: fastpg.Connection, marker: str) -> str:
    """The SQL text the server prepared for the statement containing `marker`."""
    (statement,) = await conn.select_one(
        "SELECT statement FROM pg_prepared_statements WHERE strpos(statement, $1) > 0",
        marker,
    )
    return statement


async def test_select_methods_take_params(conn: fastpg.Connection) -> None:
    sql = "SELECT name, age FROM people WHERE id = :id AND age >= :min_age"
    person = await conn.select_one(sql, params=PersonSelect(id=42, min_age=30), into=Person)
    assert_type(person, Person)
    assert person == await conn.select_one(
        "SELECT name, age FROM people WHERE id = $1 AND age >= $2",
        42,
        30,
        into=Person,
    )
    assert await conn.select_one(sql, params={"id": 42, "min_age": 30}, into=Person) == person
    row = await conn.select_one(sql, params={"id": 42, "min_age": 30})
    assert_type(row, tuple[Any, ...])
    assert row == ("person-42", 60)
    missing = await conn.select_one_or_none(sql, params={"id": 42, "min_age": 100}, into=Person)
    assert_type(missing, Person | None)
    assert missing is None
    assert await conn.select_one_or_none(sql, params={"id": 42, "min_age": 30}) == row
    people = await conn.select_many(
        "SELECT name, age FROM people WHERE id <= :last ORDER BY id",
        params={"last": 2},
        into=Person,
    )
    assert_type(people, list[Person])
    assert [p.name for p in people] == ["person-1", "person-2"]
    rows = await conn.select_many("SELECT id FROM people WHERE id <= :last", params={"last": 1})
    assert_type(rows, list[tuple[Any, ...]])
    assert rows == [(1,)]


@pytest.mark.parametrize(
    "params",
    [
        PersonSelect(id=42, min_age=30),
        RenamedSelect(id=42, min_age=30),
        DataclassSelect(id=42, min_age=30),
        SlotsSelect(id=42, min_age=30),
        ModelSelect(id=42, min_age=30),
        {"id": 42, "min_age": 30},
        types.MappingProxyType({"id": 42, "min_age": 30}),
    ],
    ids=["struct", "renamed-struct", "dataclass", "slots-dataclass", "pydantic", "dict", "mapping"],
)
async def test_every_params_type(conn: fastpg.Connection, params: fastpg.NamedParams) -> None:
    sql = "SELECT name, age FROM people WHERE id = :id AND age >= :min_age"
    assert await conn.select_one(sql, params=params) == ("person-42", 60)
    assert await conn.select_one(sql, params=params, into=Person) == Person("person-42", 60)


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT ':missing', :x::text", (":missing", "x")),
        ("SELECT 'it''s :missing', :x::text", ("it's :missing", "x")),
        ("SELECT E'\\' :missing', :x::text", ("' :missing", "x")),
        ("SELECT e'\\\\:missing', :x::text", ("\\:missing", "x")),
        ('SELECT ":missing", :x::text FROM (SELECT 1 AS ":missing") AS t', (1, "x")),
        ('SELECT "a"":missing", :x::text FROM (SELECT 1 AS "a"":missing") AS t', (1, "x")),
        ("SELECT 1 -- :missing\n, :x::text", (1, "x")),
        ("SELECT /* :missing /* :missing */ :missing */ :x::text", ("x",)),
        ("SELECT $$:missing$$, :x::text", (":missing", "x")),
        ("SELECT $tag$ :missing $$ ' $tag$, :x::text", (" :missing $$ ' ", "x")),
        ("SELECT 1::int, :x::text", (1, "x")),
        ("SELECT array_length((ARRAY[1, 2, 3])[2:3], 1), :x::text", (2, "x")),
        ("SELECT array_length((ARRAY[1, 2, 3])[2:], 1), :x::text", (2, "x")),
    ],
    ids=[
        "string",
        "string-escaped-quote",
        "escape-string",
        "escape-string-backslash",
        "quoted-identifier",
        "quoted-identifier-escaped-quote",
        "line-comment",
        "nested-block-comment",
        "dollar-quote",
        "tagged-dollar-quote",
        "cast",
        "colon-digit",
        "colon-non-name",
    ],
)
async def test_text_that_is_not_a_placeholder_survives(
    conn: fastpg.Connection,
    sql: str,
    expected: tuple[object, ...],
) -> None:
    assert await conn.select_one(sql, params={"x": "x"}) == expected


async def test_escaped_colon_is_a_literal_colon(conn: fastpg.Connection) -> None:
    sql = (
        "SELECT array_length((ARRAY[1, 2, 3])[lo\\:hi], 1), :x::text "
        "FROM (SELECT 2 AS lo, 3 AS hi) AS t"
    )
    assert await conn.select_one(sql, params={"x": "x"}) == (2, "x")
    assert await conn.select_one("SELECT '\\:x', :x::text", params={"x": "x"}) == ("\\:x", "x")
    assert await conn.select_one(
        "SELECT array_length((ARRAY[1, 2, 3])[:lo\\::hi], 1)",
        params={"lo": 1, "hi": 2},
    ) == (2,)


async def test_repeated_names_share_a_placeholder(conn: fastpg.Connection) -> None:
    sql = "SELECT :a::int + :a::int AS named_numbering, :b::text, :a::int"
    assert await conn.select_one(sql, params={"a": 1, "b": "b"}) == (2, "b", 1)
    assert await _prepared_sql(conn, "named_numbering") == (
        "SELECT $1::int + $1::int AS named_numbering, $2::text, $1::int"
    )


async def test_without_params_the_query_is_not_scanned(conn: fastpg.Connection) -> None:
    sql = "SELECT array_length((ARRAY[1, 2, 3])[lo:hi], 1) FROM (SELECT 2 AS lo, 3 AS hi) AS t"
    assert await conn.select_one(sql) == (2,)
    with pytest.raises(fastpg.ParameterError, match=":hi"):
        await conn.select_one(sql, params={})


async def test_query_without_placeholders(conn: fastpg.Connection) -> None:
    assert await conn.select_one("SELECT 1", params={}) == (1,)
    assert await conn.select_one("SELECT 1", params=PersonSelect(id=1, min_age=2)) == (1,)


async def test_unused_fields_and_keys_are_allowed(conn: fastpg.Connection) -> None:
    params = PersonSelect(id=42, min_age=30)
    assert await conn.select_one("SELECT name FROM people WHERE id = :id", params=params) == (
        "person-42",
    )
    assert await conn.select_one("SELECT :min_age::int", params=params) == (30,)
    assert await conn.select_one("SELECT :a::int", params={"a": 1, "b": 2}) == (1,)


@pytest.mark.parametrize(
    "params",
    [{"id": 42}, PersonSelect(id=42, min_age=30), ModelSelect(id=42, min_age=30)],
    ids=["dict", "struct", "pydantic"],
)
async def test_missing_name_raises_parameter_error(
    conn: fastpg.Connection,
    params: fastpg.NamedParams,
) -> None:
    sql = "SELECT name FROM people WHERE id = :id AND age >= :max_age"
    with pytest.raises(fastpg.ParameterError, match=r":max_age.*SELECT name FROM people"):
        await conn.select_one(sql, params=params)
    assert await conn.select_one("SELECT 1") == (1,)


async def test_positional_and_named_params_rejected(conn: fastpg.Connection) -> None:
    with pytest.raises(TypeError, match="not both"):
        await conn.select_one("SELECT :a::int", 1, params={"a": 1})
    with pytest.raises(TypeError, match="not both"):
        await conn.execute("SELECT :a::int", 1, params={"a": 1})


async def test_template_and_named_params_rejected(conn: fastpg.Connection) -> None:
    a = 1
    with pytest.raises(TypeError, match="t-string"):
        await conn.select_one(t"SELECT {a}::int", params={"a": 1})
    with pytest.raises(TypeError, match="t-string"):
        await conn.execute(t"SELECT {a}::int", params={"a": 1})


@pytest.mark.parametrize("value", [(1,), [1], "a", 1, PersonSelect])
async def test_unsupported_params_type_rejected(conn: fastpg.Connection, value: object) -> None:
    untyped_caller: Any = value  # what an untyped caller could pass
    with pytest.raises(TypeError, match="params= must be"):
        await conn.select_one("SELECT :a::int", params=untyped_caller)


async def test_dollar_placeholders_rejected_with_params(conn: fastpg.Connection) -> None:
    with pytest.raises(ValueError, match=r"\$n placeholder"):
        await conn.select_one("SELECT $1::int, :a::int", params={"a": 1})
    assert await conn.select_one("SELECT '$1', a$1 FROM (SELECT 1 AS a$1) AS t", params={}) == (
        "$1",
        1,
    )


async def test_null_values(conn: fastpg.Connection) -> None:
    assert await conn.select_one("SELECT :v::text, :w::int", params={"v": None, "w": None}) == (
        None,
        None,
    )
    assert await conn.select_one("SELECT :v::text", params=Item(id=1, v=None)) == (None,)


async def test_values_are_data_not_sql(conn: fastpg.Connection) -> None:
    value = "'; SELECT :x; --"
    assert await conn.select_one("SELECT :v::text", params={"v": value, "x": 1}) == (value,)


async def test_execute_returns_the_row_count(conn: fastpg.Connection) -> None:
    await conn.execute_script("CREATE TEMP TABLE named_items (id int PRIMARY KEY, v text)")
    insert = "INSERT INTO named_items VALUES (:id, :v)"
    assert await conn.execute(insert, params={"id": 1, "v": "v"}) == 1
    assert await conn.execute(insert, params=Item(id=2, v=None)) == 1
    assert await conn.execute(insert, params=ItemModel(id=3, v="v")) == 1
    assert await conn.execute("UPDATE named_items SET v = :v", params={"v": "w"}) == 3
    assert await conn.select_many("SELECT * FROM named_items ORDER BY id", into=Item) == [
        Item(1, "w"),
        Item(2, "w"),
        Item(3, "w"),
    ]
    await conn.execute_script("DROP TABLE named_items")


async def test_transaction_takes_params(conn: fastpg.Connection) -> None:
    by_id = "SELECT * FROM named_tx WHERE id = :id"
    async with conn.transaction() as tx:
        await tx.execute_script("CREATE TEMP TABLE named_tx (id int, v text) ON COMMIT DROP")
        assert await tx.execute("INSERT INTO named_tx VALUES (:id, :v)", params=Item(1, "v")) == 1
        assert await tx.select_one(by_id, params={"id": 1}) == (1, "v")
        assert await tx.select_one_or_none(by_id, params={"id": 2}, into=Item) is None
        assert await tx.select_many(by_id, params=Item(1, None), into=Item) == [Item(1, "v")]


async def test_pool_takes_params(dsn: str) -> None:
    async with await fastpg.create_pool(dsn) as pool:
        sql = "SELECT name, age FROM people WHERE id = :id AND age >= :min_age"
        person = await pool.select_one(sql, params=PersonSelect(id=42, min_age=30), into=Person)
        assert_type(person, Person)
        assert person == Person("person-42", 60)
        assert await pool.select_one(sql, params={"id": 42, "min_age": 30}) == ("person-42", 60)
        missing = await pool.select_one_or_none(sql, params={"id": 42, "min_age": 100}, into=Person)
        assert_type(missing, Person | None)
        assert missing is None
        people = await pool.select_many(
            "SELECT name, age FROM people WHERE id <= :last ORDER BY id",
            params={"last": 2},
            into=Person,
        )
        assert_type(people, list[Person])
        assert people == [Person("person-1", 19), Person("person-2", 20)]
        assert await pool.execute("SELECT generate_series(1, :n)", params={"n": 4}) == 4
        with pytest.raises(fastpg.ParameterError, match=":min_age"):
            await pool.select_one(sql, params={"id": 42})
        with pytest.raises(TypeError, match="not both"):
            await pool.select_one(sql, 42, params={"id": 42})
        a = 1
        with pytest.raises(TypeError, match="t-string"):
            await pool.execute(t"SELECT {a}::int", params={"a": 1})


async def test_pool_transaction_takes_params(dsn: str) -> None:
    insert = "INSERT INTO named_pool_tx VALUES (:id, :v)"
    async with await fastpg.create_pool(dsn) as pool, pool.transaction() as tx:
        await tx.execute_script("CREATE TEMP TABLE named_pool_tx (id int, v text) ON COMMIT DROP")
        assert await tx.execute(insert, params={"id": 1, "v": None}) == 1
        item = await tx.select_one("SELECT * FROM named_pool_tx", params={}, into=Item)
        assert item == Item(1, None)
