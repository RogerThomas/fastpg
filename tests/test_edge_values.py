"""Values PostgreSQL holds that Python can't represent, or only approximately."""

import math
from decimal import Decimal
from typing import Annotated

import msgspec
import pydantic
import pytest

import fastpg


class FloatValue(msgspec.Struct):
    value: float


class FloatValueModel(pydantic.BaseModel):
    value: float


class DecimalValue(msgspec.Struct):
    value: Decimal


class DecimalValueModel(pydantic.BaseModel):
    value: Decimal


class NonFiniteDecimalValueModel(pydantic.BaseModel):
    value: Annotated[Decimal, pydantic.Field(allow_inf_nan=True)]


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
@pytest.mark.parametrize(
    ("into", "strategy"),
    [
        (DecimalValue, "direct"),
        (DecimalValue, "msgpack"),
        (DecimalValue, "json"),
        (NonFiniteDecimalValueModel, "auto"),
    ],
)
async def test_non_finite_numeric(
    conn: fastpg.Connection,
    literal: str,
    into: type[DecimalValue | NonFiniteDecimalValueModel],
    strategy: fastpg.Strategy,
) -> None:
    query = f"SELECT '{literal}'::numeric AS value"
    row = await conn.select_one(query, into=into, strategy=strategy)
    assert str(row.value) == literal


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
@pytest.mark.parametrize("strategy", ["direct", "json"])
async def test_non_finite_numeric_into_a_pydantic_decimal_is_refused(
    conn: fastpg.Connection,
    literal: str,
    strategy: fastpg.Strategy,
) -> None:
    # pydantic's Decimal must be finite unless allow_inf_nan=True, and direct construction
    # (what "auto" picks for this model) must agree with validation.
    query = f"SELECT '{literal}'::numeric AS value"
    with pytest.raises(fastpg.DecodeError, match="finite"):
        await conn.select_one(query, into=DecimalValueModel, strategy=strategy)
    assert await conn.select_one("SELECT 1") == (1,)


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
@pytest.mark.parametrize(
    ("into", "strategy"),
    [
        (FloatValue, "direct"),
        (FloatValue, "msgpack"),
        (FloatValueModel, "direct"),
        (FloatValueModel, "json"),
    ],
)
async def test_non_finite_float8(
    conn: fastpg.Connection,
    literal: str,
    into: type[FloatValue | FloatValueModel],
    strategy: fastpg.Strategy,
) -> None:
    query = f"SELECT '{literal}'::float8 AS value"
    row = await conn.select_one(query, into=into, strategy=strategy)
    assert math.isnan(row.value) if literal == "NaN" else row.value == float(literal)


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
async def test_non_finite_float8_has_no_json_form_for_msgspec(
    conn: fastpg.Connection,
    literal: str,
) -> None:
    query = f"SELECT '{literal}'::float8 AS value"
    expected = rf'^column "value": float8 {literal} has no JSON form msgspec accepts; use'
    with pytest.raises(fastpg.DecodeError, match=expected):
        await conn.select_one(query, into=FloatValue, strategy="json")
    assert await conn.select_one("SELECT 1") == (1,)
