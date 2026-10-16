"""NULLs where the target type says there can't be one, by target and strategy."""

from typing import Annotated, Any, NamedTuple

import msgspec
import pydantic
import pytest

import fastpg
from fastpg.errors import NotNullViolationError


class AnyValue(msgspec.Struct):
    value: Any


class AnyValueTuple(NamedTuple):
    value: Any


class AnyValueModel(pydantic.BaseModel):
    value: Any


class Value(msgspec.Struct):
    value: int


class RenamedValue(msgspec.Struct, rename={"number": "value"}):
    number: int


class ConstrainedValue(msgspec.Struct):
    value: Annotated[int, msgspec.Meta(ge=0)]


class ValueTuple(NamedTuple):
    value: int


class ValueModel(pydantic.BaseModel):
    value: int


class AliasedValueModel(pydantic.BaseModel):
    number: int = pydantic.Field(alias="value")


class IdValue(msgspec.Struct):
    id: int
    value: int


class IdValueTuple(NamedTuple):
    id: int
    value: int


class IdValueModel(pydantic.BaseModel):
    id: int
    value: int


@pytest.mark.parametrize(
    ("into", "strategy"),
    [
        (AnyValue, "direct"),
        (AnyValue, "msgpack"),
        (AnyValue, "json"),
        (AnyValueTuple, "direct"),
        (AnyValueModel, "direct"),
        (AnyValueModel, "json"),
    ],
)
async def test_any_field_takes_null_under_every_strategy(
    conn: fastpg.Connection,
    into: type[AnyValue | AnyValueTuple | AnyValueModel],
    strategy: fastpg.Strategy,
) -> None:
    row = await conn.select_one("SELECT NULL::int4 AS value", into=into, strategy=strategy)
    assert row.value is None


@pytest.mark.parametrize(
    ("into", "field"),
    [
        (Value, "Value.value"),
        (RenamedValue, "RenamedValue.number"),
        (ValueTuple, "ValueTuple.value"),
        (ValueModel, "ValueModel.value"),
        (AliasedValueModel, "AliasedValueModel.number"),
    ],
)
async def test_null_into_non_optional_field_names_column_and_field(
    conn: fastpg.Connection,
    into: type[Value | RenamedValue | ValueTuple | ValueModel | AliasedValueModel],
    field: str,
) -> None:
    expected = rf'^column "value" is NULL but {field} is not optional$'
    with pytest.raises(fastpg.NullValueError, match=expected):
        await conn.select_one("SELECT NULL::int4 AS value", into=into)
    with pytest.raises(fastpg.NullValueError, match=expected):
        await conn.select_one_or_none("SELECT NULL::int4 AS value", into=into)
    assert await conn.select_one("SELECT 1") == (1,)


@pytest.mark.parametrize(
    ("into", "strategy"),
    [(Value, "msgpack"), (Value, "json"), (ConstrainedValue, "auto"), (ValueModel, "json")],
)
async def test_null_into_non_optional_field_through_validation_is_a_decode_error(
    conn: fastpg.Connection,
    into: type[Value | ConstrainedValue | ValueModel],
    strategy: fastpg.Strategy,
) -> None:
    with pytest.raises(fastpg.DecodeError, match="value"):
        await conn.select_one("SELECT NULL::int4 AS value", into=into, strategy=strategy)
    assert await conn.select_one("SELECT 1") == (1,)


@pytest.mark.xfail(
    strict=True,
    raises=fastpg.DecodeError,
    reason="open: the msgpack/json routes leave NULLs to the validating decoder, whose error"
    " becomes a plain DecodeError (notes/edge-cases-data.md)",
)
@pytest.mark.parametrize(
    ("into", "strategy"),
    [(Value, "msgpack"), (Value, "json"), (ConstrainedValue, "auto"), (ValueModel, "json")],
)
async def test_null_into_non_optional_field_is_a_null_value_error_under_every_strategy(
    conn: fastpg.Connection,
    into: type[Value | ConstrainedValue | ValueModel],
    strategy: fastpg.Strategy,
) -> None:
    with pytest.raises(fastpg.NullValueError):
        await conn.select_one("SELECT NULL::int4 AS value", into=into, strategy=strategy)


@pytest.mark.parametrize("into", [IdValue, IdValueTuple, IdValueModel])
async def test_null_in_row_1000_of_5000_fails_the_whole_select_many(
    conn: fastpg.Connection,
    into: type[IdValue | IdValueTuple | IdValueModel],
) -> None:
    # Row 1000 is in the second 512-row chunk: the first was decoded already.
    query = (
        "SELECT i AS id, CASE WHEN i = 1000 THEN NULL ELSE i END AS value"
        " FROM generate_series(1, 5000) AS s(i)"
    )
    with pytest.raises(fastpg.NullValueError, match='column "value" is NULL'):
        await conn.select_many(query, into=into)
    rows = await conn.select_many(f"{query} WHERE i <> 1000", into=into)
    assert [row.id for row in rows] == [i for i in range(1, 5001) if i != 1000]


@pytest.mark.parametrize(("into", "strategy"), [(IdValue, "msgpack"), (IdValueModel, "json")])
async def test_null_in_row_1000_of_5000_through_validation_names_the_row(
    conn: fastpg.Connection,
    into: type[IdValue | IdValueModel],
    strategy: fastpg.Strategy,
) -> None:
    query = (
        "SELECT i AS id, CASE WHEN i = 1000 THEN NULL ELSE i END AS value"
        " FROM generate_series(1, 5000) AS s(i)"
    )
    with pytest.raises(fastpg.DecodeError, match="999"):  # 0-based
        await conn.select_many(query, into=into, strategy=strategy)
    assert await conn.select_one("SELECT 1") == (1,)


async def test_null_in_tuples_and_dicts_is_none(conn: fastpg.Connection) -> None:
    query = (
        "SELECT CASE WHEN i = 1000 THEN NULL ELSE i END AS value"
        " FROM generate_series(1, 5000) AS s(i)"
    )
    tuples = await conn.select_many(query)
    dicts = await conn.select_many(query, into=dict)
    assert tuples[999] == (None,)
    assert dicts[999] == {"value": None}
    assert tuples[998] == (999,)


async def test_null_parameter_into_not_null_column(conn: fastpg.Connection) -> None:
    await conn.execute_script("CREATE TEMP TABLE required (value int NOT NULL)")
    with pytest.raises(NotNullViolationError, match='"value"'):
        await conn.execute("INSERT INTO required VALUES ($1)", None)
    assert await conn.execute("INSERT INTO required VALUES ($1)", 1) == 1
