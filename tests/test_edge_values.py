"""Values PostgreSQL holds that Python can't represent, or only approximately."""

import math

import msgspec
import pydantic
import pytest

import fastpg


class FloatValue(msgspec.Struct):
    value: float


class FloatValueModel(pydantic.BaseModel):
    value: float


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
