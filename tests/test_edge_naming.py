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
