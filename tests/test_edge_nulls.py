"""NULLs where the target type says there can't be one, by target and strategy."""

from typing import Any, NamedTuple

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


class Value(msgspec.Struct):
    value: int


class ValueTuple(NamedTuple):
    value: int


class ValueModel(pydantic.BaseModel):
    value: int


@pytest.mark.parametrize("into", [Value, ValueTuple, ValueModel])
async def test_null_into_non_optional_field_names_column_and_field(
    conn: fastpg.Connection,
    into: type[Value | ValueTuple | ValueModel],
) -> None:
    expected = rf'^column "value" is NULL but {into.__name__}\.value is not optional$'
    with pytest.raises(fastpg.NullValueError, match=expected):
        await conn.select_one("SELECT NULL::int4 AS value", into=into)
    assert await conn.select_one("SELECT 1") == (1,)
