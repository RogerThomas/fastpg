"""Column names against field names: duplicates, case, quoting, renames and aliases."""

from typing import Annotated, NamedTuple

import msgspec
import pydantic
import pytest

import fastpg


class Value(msgspec.Struct):
    value: int


class ConstrainedValue(msgspec.Struct):
    value: Annotated[int, msgspec.Meta(ge=0)]


class ValueTuple(NamedTuple):
    value: int


class ValueModel(pydantic.BaseModel):
    value: int


class ConstrainedValueModel(pydantic.BaseModel):
    value: int = pydantic.Field(ge=0)


class StrictValue(msgspec.Struct, forbid_unknown_fields=True):
    value: int


class StrictValueModel(pydantic.BaseModel, extra="forbid"):
    value: int


class WithDefault(msgspec.Struct):
    value: int
    other: int = 9


class WithDefaultTuple(NamedTuple):
    value: int
    other: int = 9


class WithDefaultModel(pydantic.BaseModel):
    value: int
    other: int = 9


class Lower(msgspec.Struct):
    name: str


class Upper(msgspec.Struct):
    Name: str


class Unusual(msgspec.Struct, rename={"a": "first name", "b": "it's", "c": "größe ✓"}):
    """msgspec refuses '"' and '\\' in renamed names; dict rows take any column name."""

    a: int
    b: int
    c: int


class Camel(msgspec.Struct, rename="camel"):
    item_id: int


class Long(msgspec.Struct, rename={"value": "a" * 70}):
    value: int


class AliasedModel(pydantic.BaseModel):
    first_name: str = pydantic.Field(alias="first name")


class AliasOrNameModel(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(populate_by_name=True)

    first_name: str = pydantic.Field(alias="first name")


class NameOnlyModel(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(validate_by_name=True, validate_by_alias=False)

    first_name: str = pydantic.Field(alias="first name")


@pytest.mark.parametrize("strategy", ["auto", "json"])
@pytest.mark.parametrize(
    ("into", "column"),
    [
        (AliasedModel, '"first name"'),
        (AliasOrNameModel, '"first name"'),
        (AliasOrNameModel, "first_name"),
        (NameOnlyModel, "first_name"),
    ],
)
async def test_pydantic_matches_columns_as_its_validation_does(
    conn: fastpg.Connection,
    into: type[AliasedModel | AliasOrNameModel | NameOnlyModel],
    column: str,
    strategy: fastpg.Strategy,
) -> None:
    row = await conn.select_one(f"SELECT 'x' AS {column}", into=into, strategy=strategy)
    assert row.first_name == "x"


@pytest.mark.parametrize(
    ("into", "column"),
    [(AliasedModel, "first_name"), (NameOnlyModel, '"first name"')],
)
async def test_pydantic_column_its_validation_doesnt_match(
    conn: fastpg.Connection,
    into: type[AliasedModel | NameOnlyModel],
    column: str,
) -> None:
    with pytest.raises(fastpg.DecodeError, match="first"):
        await conn.select_one(f"SELECT 'x' AS {column}", into=into)


async def test_pydantic_model_matching_names_and_aliases_is_not_built_directly(
    conn: fastpg.Connection,
) -> None:
    expected = r"AliasOrNameModel: .*validate_by_name"
    with pytest.raises(fastpg.UnsupportedTypeError, match=expected):
        await conn.select_one("SELECT 'x' AS first_name", into=AliasOrNameModel, strategy="direct")


@pytest.mark.parametrize(
    ("into", "strategy"),
    [
        (dict, "direct"),
        (Value, "direct"),
        (Value, "msgpack"),
        (Value, "json"),
        (ConstrainedValue, "auto"),
        (ValueTuple, "direct"),
        (ValueModel, "direct"),
        (ValueModel, "json"),
        (ConstrainedValueModel, "auto"),
    ],
)
async def test_duplicate_column_for_a_field_is_refused_by_every_target(
    conn: fastpg.Connection,
    into: type[object],
    strategy: fastpg.Strategy,
) -> None:
    # Keeping either value would silently drop the other.
    query = "SELECT 1 AS value, 2 AS value"
    expected = r'^column "value" appears more than once in the result'
    with pytest.raises(fastpg.TypeMismatchError, match=expected):
        await conn.select_one(query, into=into, strategy=strategy)
    with pytest.raises(fastpg.TypeMismatchError, match=expected):  # even with no rows
        await conn.select_many(f"{query} WHERE false", into=into, strategy=strategy)


@pytest.mark.parametrize("strategy", ["direct", "msgpack", "json"])
async def test_duplicate_column_no_field_reads_is_ignored(
    conn: fastpg.Connection,
    strategy: fastpg.Strategy,
) -> None:
    query = "SELECT 1 AS value, 2 AS other, 3 AS other"
    assert await conn.select_one(query, into=Value, strategy=strategy) == Value(1)


async def test_duplicate_columns_in_tuples_keep_every_value(conn: fastpg.Connection) -> None:
    assert await conn.select_one("SELECT 1 AS value, 2 AS value") == (1, 2)


@pytest.mark.parametrize("strategy", ["direct", "msgpack", "json"])
async def test_unquoted_column_names_fold_to_lower_case(
    conn: fastpg.Connection,
    strategy: fastpg.Strategy,
) -> None:
    assert await conn.select_one("SELECT 'x' AS Name", into=dict) == {"name": "x"}
    assert await conn.select_one("SELECT 'x' AS Name", into=Lower, strategy=strategy) == Lower("x")
    upper = await conn.select_one("SELECT 'x' AS \"Name\"", into=Upper, strategy=strategy)
    assert upper == Upper("x")
    with pytest.raises(fastpg.DecodeError, match="Name"):
        await conn.select_one("SELECT 'x' AS Name", into=Upper, strategy=strategy)


@pytest.mark.parametrize("strategy", ["direct", "msgpack", "json"])
async def test_columns_named_with_spaces_quotes_and_unicode(
    conn: fastpg.Connection,
    strategy: fastpg.Strategy,
) -> None:
    query = 'SELECT 1 AS "first name", 2 AS "it\'s", 3 AS "größe ✓", 4 AS "say ""hi""", 5 AS "a\\b"'
    row = await conn.select_one(query, into=Unusual, strategy=strategy)
    assert row == Unusual(1, 2, 3)
    assert await conn.select_one(query, into=dict) == {
        "first name": 1,
        "it's": 2,
        "größe ✓": 3,
        'say "hi"': 4,
        "a\\b": 5,
    }


@pytest.mark.parametrize("strategy", ["direct", "msgpack", "json"])
async def test_struct_rename_camel(conn: fastpg.Connection, strategy: fastpg.Strategy) -> None:
    query = 'SELECT 1 AS "itemId", 2 AS item_id'
    assert await conn.select_one(query, into=Camel, strategy=strategy) == Camel(1)


@pytest.mark.parametrize("strategy", ["direct", "msgpack", "json"])
async def test_column_name_beyond_63_bytes_is_truncated_by_the_server(
    conn: fastpg.Connection,
    strategy: fastpg.Strategy,
) -> None:
    # PostgreSQL truncates identifiers to 63 bytes (with a NOTICE), so a field named with
    # the full 70 characters finds no column.
    query = f'SELECT 1 AS "{"a" * 70}"'
    with pytest.raises(fastpg.DecodeError, match="a" * 70):
        await conn.select_one(query, into=Long, strategy=strategy)
    assert await conn.select_one(query, into=dict) == {"a" * 63: 1}


@pytest.mark.parametrize(
    ("into", "strategy", "error"),
    [
        (StrictValue, "direct", fastpg.UnknownColumnError),
        (StrictValue, "msgpack", fastpg.DecodeError),
        (StrictValue, "json", fastpg.DecodeError),
        (StrictValueModel, "direct", fastpg.UnknownColumnError),
        (StrictValueModel, "json", fastpg.DecodeError),
    ],
)
async def test_extra_column_refused_by_types_that_forbid_unknown_fields(
    conn: fastpg.Connection,
    into: type[StrictValue | StrictValueModel],
    strategy: fastpg.Strategy,
    error: type[fastpg.DecodeError],
) -> None:
    with pytest.raises(error, match="other"):
        await conn.select_one("SELECT 1 AS value, 2 AS other", into=into, strategy=strategy)
    row = await conn.select_one("SELECT 1 AS value", into=into, strategy=strategy)
    assert row.value == 1


@pytest.mark.parametrize(
    ("into", "strategy"),
    [
        (Value, "direct"),
        (Value, "msgpack"),
        (Value, "json"),
        (ValueTuple, "direct"),
        (ValueModel, "direct"),
        (ValueModel, "json"),
    ],
)
async def test_extra_columns_are_ignored_by_default(
    conn: fastpg.Connection,
    into: type[Value | ValueTuple | ValueModel],
    strategy: fastpg.Strategy,
) -> None:
    row = await conn.select_one("SELECT 2 AS other, 1 AS value", into=into, strategy=strategy)
    assert row.value == 1


@pytest.mark.parametrize(
    ("into", "strategy"),
    [
        (WithDefault, "direct"),
        (WithDefault, "msgpack"),
        (WithDefault, "json"),
        (WithDefaultTuple, "direct"),
        (WithDefaultModel, "direct"),
        (WithDefaultModel, "json"),
    ],
)
async def test_missing_column_with_a_default(
    conn: fastpg.Connection,
    into: type[WithDefault | WithDefaultTuple | WithDefaultModel],
    strategy: fastpg.Strategy,
) -> None:
    row = await conn.select_one("SELECT 1 AS value", into=into, strategy=strategy)
    assert (row.value, row.other) == (1, 9)


@pytest.mark.parametrize(
    ("into", "strategy", "error"),
    [
        (WithDefault, "direct", fastpg.MissingColumnError),
        (WithDefault, "msgpack", fastpg.DecodeError),
        (WithDefault, "json", fastpg.DecodeError),
        (WithDefaultTuple, "direct", fastpg.MissingColumnError),
        (WithDefaultModel, "direct", fastpg.MissingColumnError),
        (WithDefaultModel, "json", fastpg.DecodeError),
    ],
)
async def test_missing_column_without_a_default(
    conn: fastpg.Connection,
    into: type[WithDefault | WithDefaultTuple | WithDefaultModel],
    strategy: fastpg.Strategy,
    error: type[fastpg.DecodeError],
) -> None:
    with pytest.raises(error, match="value"):
        await conn.select_one("SELECT 1 AS other", into=into, strategy=strategy)


@pytest.mark.parametrize("into", [WithDefault, WithDefaultTuple, WithDefaultModel])
async def test_missing_column_without_a_default_raises_for_an_empty_result(
    conn: fastpg.Connection,
    into: type[WithDefault | WithDefaultTuple | WithDefaultModel],
) -> None:
    # Columns are bound to fields before any row is decoded.
    with pytest.raises(fastpg.MissingColumnError, match="value"):
        await conn.select_many("SELECT 1 AS other WHERE false", into=into)


@pytest.mark.xfail(
    strict=True,
    reason="open: the msgpack/json routes only see the columns through the rows they encode"
    " (notes/edge-cases-data.md)",
)
@pytest.mark.parametrize(
    ("into", "strategy"),
    [(WithDefault, "msgpack"), (WithDefaultModel, "json")],
)
async def test_missing_column_without_a_default_raises_for_an_empty_result_through_validation(
    conn: fastpg.Connection,
    into: type[WithDefault | WithDefaultModel],
    strategy: fastpg.Strategy,
) -> None:
    with pytest.raises(fastpg.DecodeError):
        await conn.select_many("SELECT 1 AS other WHERE false", into=into, strategy=strategy)
