"""Arrays: one-dimensional arrays of every scalar type, read into lists and sent from lists."""

import datetime as dt
import gc
import typing
import uuid
from decimal import Decimal
from typing import Annotated, Any, NamedTuple

import msgspec
import pydantic
import pytest

import fastpg

UUID = uuid.UUID("28dd2c79-55ce-9264-5624-0b2ff0100bde")
NOON = dt.datetime(2024, 2, 29, 12, 0, 0, 1)  # noqa: DTZ001 - naive on purpose

# (element type, Python type of its values, values with a NULL), for every scalar type.
ELEMENTS: list[tuple[str, Any, list[Any]]] = [
    ("bool", bool, [True, None, False]),
    ("int2", int, [-32768, None, 32767]),
    ("int4", int, [3, None, 2_147_483_647]),
    ("int8", int, [-9_223_372_036_854_775_808, None, 42]),
    ("oid", int, [0, None, 4_294_967_295]),
    ("float4", float, [1.5, None, -0.25]),
    ("float8", float, [3.141592653589793, None, -1e300]),
    ("numeric", Decimal, [Decimal("28.490"), None, Decimal("-10000.0005")]),
    ("text", str, ["", None, "héllo wörld ✓"]),
    ("varchar", str, ["abc", None]),
    ("bpchar", str, ["ab", None]),
    ("name", str, ["pg_class", None]),
    ('"char"', str, ["a", None, ""]),
    ("bytea", bytes, [b"\x00\x01\xff", None, b""]),
    ("uuid", uuid.UUID, [UUID, None]),
    ("date", dt.date, [dt.date(2000, 1, 1), None, dt.date(1, 1, 1)]),
    ("time", dt.time, [dt.time(23, 59, 59, 999_999), None]),
    ("timestamp", dt.datetime, [NOON, None]),
    ("timestamptz", dt.datetime, [dt.datetime(2024, 1, 1, 0, 1, 1, tzinfo=dt.UTC), None]),
    ("json", Any, [{"a": [1, None]}, None, [1, "two"]]),
    ("jsonb", Any, [{"a": [1, None]}, None, [1, "two"]]),
]


def _id(element: tuple[str, Any, list[Any]]) -> str:
    return element[0].strip('"')


class Holder(NamedTuple):
    """Builds the same one-field target, `value: list[X | None]`, of every kind."""

    name: str
    strategy: fastpg.Strategy
    build: typing.Callable[[Any], type[Any]]


def _struct(item: Any) -> type[Any]:  # noqa: ANN401 - any element type
    return msgspec.defstruct("Value", [("value", list[item | None])])


def _named_tuple(item: Any) -> type[Any]:  # noqa: ANN401
    return NamedTuple("ValueTuple", [("value", list[item | None])])


def _model(item: Any) -> type[Any]:  # noqa: ANN401
    config = pydantic.ConfigDict(val_json_bytes="base64")  # the json route's bytes
    return pydantic.create_model("ValueModel", __config__=config, value=list[item | None])


HOLDERS = [
    Holder("struct", "direct", _struct),
    Holder("struct", "msgpack", _struct),
    Holder("struct", "json", _struct),
    Holder("named-tuple", "auto", _named_tuple),
    Holder("pydantic", "direct", _model),
    Holder("pydantic", "json", _model),
]


def _value(row: object) -> object:
    if isinstance(row, dict):
        return typing.cast("dict[str, object]", row)["value"]
    return getattr(row, "value")  # noqa: B009 - a Struct, NamedTuple or model


def _assert_same(got: object, expected: list[Any]) -> None:
    """Equal, element by element, and of the same types."""
    assert isinstance(got, list)
    elements = typing.cast("list[object]", got)
    assert elements == expected
    assert [type(e) for e in elements] == [type(e) for e in expected]


@pytest.mark.parametrize("element", ELEMENTS, ids=_id)
async def test_roundtrip_in_tuple_and_dict_rows(
    conn: fastpg.Connection,
    element: tuple[str, Any, list[Any]],
) -> None:
    pg_type, _, values = element
    query = f"SELECT $1::{pg_type}[] AS value"
    for _ in range(2):  # the statement's first use (parameter types unknown), then cached
        (got,) = await conn.select_one(query, values)
        _assert_same(got, values)
        _assert_same(_value(await conn.select_one(query, values, into=dict)), values)
    (got,) = await conn.select_one(query, tuple(values))
    _assert_same(got, values)


@pytest.mark.parametrize("holder", HOLDERS, ids=lambda h: f"{h.name}-{h.strategy}")
@pytest.mark.parametrize("element", ELEMENTS, ids=_id)
async def test_roundtrip_into_list_fields(
    conn: fastpg.Connection,
    element: tuple[str, Any, list[Any]],
    holder: Holder,
) -> None:
    pg_type, item, values = element
    into = holder.build(item)
    query = f"SELECT $1::{pg_type}[] AS value"
    row = await conn.select_one(query, values, into=into, strategy=holder.strategy)
    _assert_same(_value(row), values)
    empty = await conn.select_one(query, [], into=into, strategy=holder.strategy)
    assert _value(empty) == []


@pytest.mark.parametrize("holder", HOLDERS, ids=lambda h: f"{h.name}-{h.strategy}")
async def test_null_array_needs_an_optional_field(conn: fastpg.Connection, holder: Holder) -> None:
    into = holder.build(int)
    with pytest.raises(fastpg.DecodeError):  # NullValueError, or a validation error
        await conn.select_one("SELECT NULL::int[] AS value", into=into, strategy=holder.strategy)


class Tagged(msgspec.Struct):
    id: int
    tags: list[str] | None
    scores: list[float]


class TaggedTuple(NamedTuple):
    id: int
    tags: list[str] | None
    scores: list[float]


class TaggedModel(pydantic.BaseModel):
    id: int
    tags: list[str] | None
    scores: list[float]


@pytest.mark.parametrize(
    ("into", "strategy"),
    [
        (Tagged, "direct"),
        (Tagged, "msgpack"),
        (Tagged, "json"),
        (TaggedTuple, "auto"),
        (TaggedModel, "direct"),
        (TaggedModel, "json"),
    ],
)
async def test_null_and_empty_arrays(
    conn: fastpg.Connection,
    into: type[Tagged | TaggedTuple | TaggedModel],
    strategy: fastpg.Strategy,
) -> None:
    query = "SELECT 1 AS id, NULL::text[] AS tags, '{}'::int[] AS scores"
    row = await conn.select_one(query, into=into, strategy=strategy)
    assert (row.id, row.tags, row.scores) == (1, None, [])
    # int elements widen to float, as for a float field.
    query = "SELECT 1 AS id, '{}'::text[] AS tags, ARRAY[1, 2] AS scores"
    row = await conn.select_one(query, into=into, strategy=strategy)
    assert (row.tags, row.scores) == ([], [1.0, 2.0])
    assert all(type(s) is float for s in row.scores)


async def test_null_element_needs_an_optional_item_type(conn: fastpg.Connection) -> None:
    query = "SELECT 1 AS id, ARRAY['a', NULL] AS tags, '{}'::int[] AS scores"
    expected = (
        r'^column "tags" has a NULL element \(1\) but Tagged.tags is list\[str\], '
        r"not list\[str \| None\]$"
    )
    with pytest.raises(fastpg.NullValueError, match=expected):
        await conn.select_one(query, into=Tagged)
    with pytest.raises(fastpg.NullValueError, match=r"TaggedTuple\.tags is list"):
        await conn.select_one(query, into=TaggedTuple)
    with pytest.raises(fastpg.NullValueError, match=r"TaggedModel\.tags is list"):
        await conn.select_one(query, into=TaggedModel)
    with pytest.raises(fastpg.DecodeError):  # msgspec's own validation
        await conn.select_one(query, into=Tagged, strategy="msgpack")
    assert await conn.select_one(query) == (1, ["a", None], [])


class Numbers(msgspec.Struct):
    value: list[int]


class Anything(msgspec.Struct):
    value: Any


class Number(msgspec.Struct):
    value: int


class Mapping(msgspec.Struct):
    value: dict[str, int]


class Bounded(msgspec.Struct):
    value: Annotated[list[int], msgspec.Meta(max_length=2)]


@pytest.mark.parametrize(
    ("expression", "into", "expected"),
    [
        ("ARRAY['a']", Numbers, r"type text\[\], but Numbers.value expects list\[int\]$"),
        ("ARRAY[1.5]", Numbers, r"type numeric\[\], but Numbers.value expects list\[int\]$"),
        ("1", Numbers, r"type int4, but Numbers.value expects list\[int\]$"),
        ("ARRAY[1]", Number, r"type int4\[\], but Number.value expects int$"),
        ("ARRAY[1]", Mapping, r"type int4\[\], but Mapping.value expects a JSON-decoded type$"),
        ("ARRAY[1]", Bounded, r"type int4\[\], but Bounded.value expects a JSON-decoded type$"),
    ],
)
async def test_wrong_element_type_is_a_type_mismatch(
    conn: fastpg.Connection,
    expression: str,
    into: type[msgspec.Struct],
    expected: str,
) -> None:
    with pytest.raises(fastpg.TypeMismatchError, match=expected):
        await conn.select_one(f"SELECT {expression} AS value", into=into)
    with pytest.raises(fastpg.TypeMismatchError, match=expected):  # even with no rows
        await conn.select_many(f"SELECT {expression} AS value WHERE false", into=into)


async def test_constrained_list_validates_through_msgpack(conn: fastpg.Connection) -> None:
    query = "SELECT ARRAY[1, 2, 3] AS value"
    with pytest.raises(fastpg.DecodeError, match="length <= 2"):
        await conn.select_one(query, into=Bounded, strategy="msgpack")
    row = await conn.select_one("SELECT ARRAY[1, 2] AS value", into=Bounded, strategy="msgpack")
    assert row.value == [1, 2]


async def test_any_field_takes_an_array(conn: fastpg.Connection) -> None:
    row = await conn.select_one("SELECT ARRAY[1, NULL] AS value", into=Anything)
    assert row.value == [1, None]


@pytest.mark.parametrize(
    ("expression", "message"),
    [
        ("'{{1, 2}, {3, 4}}'::int[]", r"multi-dimensional arrays are not supported"),
        ("'{{}}'::int[]", r"^"),  # an empty 2-D literal is just empty: '{}'
        ("'[0:1]={1, 2}'::int[]", r"arrays indexed from other than 1 are not supported"),
    ],
)
@pytest.mark.parametrize("strategy", ["direct", "msgpack", "json"])
async def test_other_shapes_are_a_clear_error(
    conn: fastpg.Connection,
    expression: str,
    message: str,
    strategy: fastpg.Strategy,
) -> None:
    query = f"SELECT {expression} AS value"
    if expression == "'{{}}'::int[]":
        row = await conn.select_one(query, into=Numbers, strategy=strategy)
        assert row.value == []
        return
    expected = rf'^column "value" \(int4\[\]\): {message}'
    if strategy != "direct":
        expected = rf'^column "value": {message}'
    with pytest.raises(fastpg.DecodeError, match=expected):
        await conn.select_one(query, into=Numbers, strategy=strategy)
    if strategy == "direct":
        with pytest.raises(fastpg.DecodeError, match=message):
            await conn.select_one(query)
        with pytest.raises(fastpg.DecodeError, match=message):
            await conn.select_one(query, into=dict)
    assert await conn.select_one("SELECT 1") == (1,)


async def test_unsupported_element_type_names_the_array_type(conn: fastpg.Connection) -> None:
    expected = r'^column "value" has PostgreSQL type interval\[\], which fastpg cannot decode$'
    with pytest.raises(fastpg.UnsupportedTypeError, match=expected):
        await conn.select_one("SELECT ARRAY['1 day'::interval] AS value", into=Anything)


async def test_enum_and_domain_elements(conn: fastpg.Connection) -> None:
    await conn.execute_script(
        "CREATE TYPE pg_temp.mood AS ENUM ('sad', 'happy');"
        "CREATE DOMAIN pg_temp.positive AS int CHECK (VALUE > 0)",
    )
    query = "SELECT $1::pg_temp.mood[], $2::pg_temp.positive[]"
    for _ in range(2):
        assert await conn.select_one(query, ["happy", None], [1, 2]) == (["happy", None], [1, 2])


class Doc(msgspec.Struct):
    value: list[dict[str, int]]


class DocText(msgspec.Struct):
    value: list[str | None]


async def test_json_elements(conn: fastpg.Connection) -> None:
    query = """SELECT ARRAY['{"a": 1}', '{"b": 2}']::jsonb[] AS value"""
    assert (await conn.select_one(query, into=Doc)).value == [{"a": 1}, {"b": 2}]
    # A str item takes the JSON text, as a str field does.
    query = """SELECT ARRAY['{"a": 1}', NULL]::jsonb[] AS value"""
    assert (await conn.select_one(query, into=DocText)).value == ['{"a": 1}', None]
    with pytest.raises(fastpg.DecodeError):  # validated by the field's decoder
        await conn.select_one("""SELECT ARRAY['{"a": "x"}']::jsonb[] AS value""", into=Doc)


class PriceModel(pydantic.BaseModel):
    value: list[Decimal]


async def test_pydantic_decimal_elements_must_be_finite(conn: fastpg.Connection) -> None:
    query = "SELECT ARRAY[1.5, 'NaN']::numeric[] AS value"
    with pytest.raises(fastpg.DecodeError, match="element 1 is NaN, but pydantic Decimals"):
        await conn.select_one(query, into=PriceModel)
    with pytest.raises(fastpg.DecodeError):
        await conn.select_one(query, into=PriceModel, strategy="json")


# --- Parameters -----------------------------------------------------------------------------


async def test_any_with_a_list_or_tuple(conn: fastpg.Connection) -> None:
    query = "SELECT id FROM people WHERE id = ANY($1) ORDER BY id"
    for ids in ([3, 8, 42], (3, 8, 42), [3, 8, 42]):
        assert await conn.select_many(query, ids) == [(3,), (8,), (42,)]
    assert await conn.select_many(query, []) == []
    uuids = "SELECT id FROM people WHERE external_id = ANY($1)"
    assert await conn.select_many(uuids, [uuid.UUID(int=0)]) == []


async def test_list_for_json_is_still_json(conn: fastpg.Connection) -> None:
    for _ in range(2):  # first use and cached
        assert await conn.select_one("SELECT $1::jsonb", [1, "two"]) == ([1, "two"],)
    assert await conn.select_one("SELECT $1::jsonb[]", [[1], {"a": 2}]) == ([[1], {"a": 2}],)


@pytest.mark.parametrize(
    ("query", "value", "expected"),
    [
        ("SELECT $1::int[]", ["1"], r"element 0: cannot encode str as PostgreSQL type int4$"),
        ("SELECT $1::int[]", [1, 2**40], r"element 1: int out of range for PostgreSQL type int4$"),
        ("SELECT $1::int[]", [[1, 2]], r"element 0: multi-dimensional arrays are not supported"),
        ("SELECT $1::int[]", [object()], r"unsupported"),  # first use: as JSON
        ("SELECT $1::jsonb", (1, 2), r"cannot encode list/tuple \(array\) as .* type jsonb"),
        ("SELECT $1::text", (1, 2), r"cannot encode list/tuple \(array\) as .* type text"),
    ],
)
async def test_parameter_errors_name_the_element(
    conn: fastpg.Connection,
    query: str,
    value: object,
    expected: str,
) -> None:
    for _ in range(2):  # first use and cached
        with pytest.raises(fastpg.ParameterError, match=rf"^parameter \$1: .*{expected}"):
            await conn.select_one(query, value)
    assert await conn.select_one("SELECT 1") == (1,)


# --- One-to-many with array_agg -------------------------------------------------------------


class AgeGroup(msgspec.Struct):
    age: int
    ids: list[int]
    names: list[str]
    emails: list[str | None]


async def test_array_agg_one_to_many(conn: fastpg.Connection) -> None:
    query = """
SELECT age, array_agg(id ORDER BY id) AS ids, array_agg(name ORDER BY id) AS names,
       array_agg(email ORDER BY id) AS emails
FROM people WHERE id <= $1 GROUP BY age ORDER BY age
"""
    groups = await conn.select_many(query, 120, into=AgeGroup)
    assert len(groups) == 60
    (sixty,) = (group for group in groups if group.age == 60)
    assert sixty.ids == [42, 102]
    assert sixty.names == ["person-42", "person-102"]
    assert sixty.emails == [None, "person102@example.com"]
    for strategy in ("msgpack", "json"):
        assert await conn.select_many(query, 120, into=AgeGroup, strategy=strategy) == groups
    # Rows holding lists stay visible to the cyclic GC.
    assert all(gc.is_tracked(group) for group in groups)
    assert all(gc.is_tracked(row) for row in await conn.select_many(query, 120))


async def test_pool_and_transaction(dsn: str) -> None:
    query = "SELECT array_agg(id ORDER BY id) AS ids FROM people WHERE id = ANY($1)"
    async with await fastpg.create_pool(dsn, max_size=2) as pool:
        assert await pool.select_one(query, [5, 6]) == ([5, 6],)
        assert await pool.select_one(query, [7], into=dict) == {"ids": [7]}
        async with pool.transaction() as tx:
            await tx.execute_script("CREATE TEMP TABLE tags_tx (id int, tags text[])")
            await tx.execute("INSERT INTO tags_tx VALUES ($1, $2)", 1, ["a", None])
            assert await tx.select_one("SELECT tags FROM tags_tx") == (["a", None],)


# --- Bulk writes ----------------------------------------------------------------------------


class TagRow(msgspec.Struct):
    id: int
    tags: list[str | None] | None
    scores: list[float]


@pytest.mark.parametrize("method", ["execute_many", "insert_many", "copy_in"])
async def test_bulk_writes_take_lists(conn: fastpg.Connection, method: str) -> None:
    await conn.execute_script(
        "DROP TABLE IF EXISTS pg_temp.tag_rows;"
        "CREATE TEMP TABLE tag_rows (id int, tags text[], scores float8[])",
    )
    rows: list[tuple[int, list[str | None] | None, list[float]]] = [
        (1, ["a", None], [1.5]),
        (2, None, []),
        (3, [], [2.0, 3.0]),
    ]
    if method == "execute_many":
        count = await conn.execute_many("INSERT INTO tag_rows VALUES ($1, $2, $3)", rows)
    elif method == "insert_many":
        count = await conn.insert_many("tag_rows", rows)
    else:
        count = await conn.copy_in("tag_rows", rows)
    assert count == 3
    got = await conn.select_many("SELECT * FROM tag_rows ORDER BY id", into=TagRow)
    assert got == [TagRow(*row) for row in rows]
    with pytest.raises(fastpg.ParameterError, match="element 0: cannot encode str"):
        await conn.copy_in("tag_rows", [(4, ["a"], ["x"])])
