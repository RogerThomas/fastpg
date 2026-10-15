"""pydantic v2 targets: native construction ("direct") and validate_json ("json")."""

import datetime as dt
import uuid
from decimal import Decimal
from typing import Annotated, Any

import msgspec
import pytest
from pydantic import BaseModel, ConfigDict, Field, field_validator

import fastpg


class PersonModel(BaseModel):
    id: int
    name: str
    age: int
    email: str | None
    score: float
    balance: Decimal
    active: bool
    external_id: uuid.UUID
    created_at: dt.datetime
    birthday: dt.date | None


class Validated(BaseModel):
    name: str

    @field_validator("name")
    @classmethod
    def shout(cls, v: str) -> str:
        return v.upper()


class Old(BaseModel):
    age: Annotated[int, Field(ge=70)]


class Aliased(BaseModel):
    model_config = ConfigDict(frozen=True)

    ext: uuid.UUID = Field(alias="external_id")
    nickname: str = "anon"
    flag: bool | None = None


class Tagged(BaseModel):
    name: str
    tags: list[str] = []  # pydantic copies mutable defaults per instance


class DefaultsBetween(BaseModel):
    nickname: str = "nickname"
    name: str
    flag: bool | None = None
    age: int


class StrictEmail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str


class LooseEmail(BaseModel):
    email: str


class PointModel(BaseModel):
    x: int
    y: int


class DocModel(BaseModel):
    points: list[PointModel]
    meta: dict[str, int] | None


class JSONAsTextModel(BaseModel):
    doc: str
    meta: str | None


class JSONDecodedModel(BaseModel):
    doc: dict[str, int]
    meta: Any


class PersonStruct(msgspec.Struct):
    id: int
    name: str
    balance: Decimal
    external_id: uuid.UUID
    created_at: dt.datetime
    birthday: dt.date | None


async def test_direct_builds_real_models(conn: fastpg.Connection) -> None:
    person = await conn.select_one(
        "SELECT * FROM people WHERE id = $1",
        77,
        into=PersonModel,
        strategy="direct",
    )
    assert type(person) is PersonModel
    assert person.external_id == uuid.UUID("28dd2c79-55ce-9264-5624-0b2ff0100bde")
    assert person.balance == Decimal("28.49")
    assert person.email is None
    assert person.model_fields_set == set(PersonModel.model_fields)
    assert person.model_extra is None
    # Behaves like a validated instance: dump, re-validate, assignment.
    assert PersonModel.model_validate(person.model_dump()) == person
    assert PersonModel.model_validate_json(person.model_dump_json()) == person
    person.name = "renamed"
    assert person.name == "renamed"


@pytest.mark.parametrize("strategy", ["direct", "json"])
async def test_strategies_agree_with_validation(
    conn: fastpg.Connection,
    strategy: fastpg.Strategy,
) -> None:
    sql = "SELECT * FROM people WHERE id BETWEEN $1 AND $2 ORDER BY id"
    rows = await conn.select_many(sql, 1, 2500, into=PersonModel, strategy=strategy)
    tuples = await conn.select_many(sql, 1, 2500)
    fields = list(PersonModel.model_fields)
    expected = [PersonModel.model_validate(dict(zip(fields, t, strict=True))) for t in tuples]
    assert rows == expected


async def test_auto_prefers_direct_for_plain_models(conn: fastpg.Connection) -> None:
    person = await conn.select_one("SELECT * FROM people WHERE id = $1", 5, into=PersonModel)
    # model_validate would have produced pydantic's TzInfo; direct keeps datetime.UTC.
    assert person.created_at.tzinfo is dt.UTC


async def test_validators_force_json(conn: fastpg.Connection) -> None:
    got = await conn.select_one("SELECT name FROM people WHERE id = 3", into=Validated)
    assert got.name == "PERSON-3"
    with pytest.raises(fastpg.UnsupportedTypeError, match="validators"):
        await conn.select_one("SELECT 'x' AS name", into=Validated, strategy="direct")


async def test_constraints_are_validated(conn: fastpg.Connection) -> None:
    assert (await conn.select_one("SELECT age FROM people WHERE id = 52", into=Old)).age == 70
    with pytest.raises(fastpg.DecodeError, match="greater than or equal to 70") as exc:
        await conn.select_one("SELECT age FROM people WHERE id = 3", into=Old)
    assert type(exc.value.__cause__).__name__ == "ValidationError"


async def test_aliases_and_defaults(conn: fastpg.Connection) -> None:
    sql = "SELECT external_id FROM people WHERE id = 1"
    direct = await conn.select_one(sql, into=Aliased, strategy="direct")
    assert direct == await conn.select_one(sql, into=Aliased, strategy="json")
    assert (direct.nickname, direct.flag) == ("anon", None)
    assert direct.model_fields_set == {"ext"}
    assert hash(direct) == hash(direct.model_copy())


async def test_direct_dicts_are_per_instance_and_in_field_order(conn: fastpg.Connection) -> None:
    sql = "SELECT age, name FROM people WHERE id <= 2 ORDER BY id"
    rows = await conn.select_many(sql, into=DefaultsBetween, strategy="direct")
    assert [list(row.__dict__) for row in rows] == [["nickname", "name", "flag", "age"]] * 2
    assert [row.model_fields_set for row in rows] == [{"name", "age"}] * 2
    rows[0].nickname = "changed"
    assert (rows[0].nickname, rows[1].nickname) == ("changed", "nickname")
    assert rows[1] == DefaultsBetween(name="person-2", age=20)


async def test_mutable_default_falls_back_to_json(conn: fastpg.Connection) -> None:
    got = await conn.select_one("SELECT name FROM people WHERE id = 3", into=Tagged)
    assert got.tags == []
    with pytest.raises(fastpg.UnsupportedTypeError, match="mutable default"):
        await conn.select_one("SELECT 'x' AS name", into=Tagged, strategy="direct")


async def test_direct_errors(conn: fastpg.Connection) -> None:
    with pytest.raises(fastpg.NullValueError, match=r"StrictEmail\.email"):
        await conn.select_one("SELECT email FROM people WHERE id = 7", into=StrictEmail)
    with pytest.raises(fastpg.UnknownColumnError, match=r'"id" .*\(extra="forbid"\)$'):
        await conn.select_one("SELECT email, id FROM people WHERE id = 1", into=StrictEmail)
    with pytest.raises(fastpg.MissingColumnError, match="email"):
        await conn.select_one("SELECT id FROM people WHERE id = 1", into=LooseEmail)
    with pytest.raises(fastpg.TypeMismatchError, match="expects str"):
        await conn.select_one("SELECT 1 AS email", into=StrictEmail)
    with pytest.raises(fastpg.DecodeError, match="email"):
        await conn.select_one(
            "SELECT email FROM people WHERE id = 7",
            into=StrictEmail,
            strategy="json",
        )


async def test_json_fields(conn: fastpg.Connection) -> None:
    sql = """SELECT '[{"x": 1, "y": 2}]'::jsonb AS points, '{"a": 1}'::json AS meta"""
    direct = await conn.select_one(sql, into=DocModel, strategy="direct")
    assert direct == DocModel(points=[PointModel(x=1, y=2)], meta={"a": 1})
    assert direct == await conn.select_one(sql, into=DocModel, strategy="json")


@pytest.mark.parametrize("strategy", ["direct", "json"])
@pytest.mark.parametrize("json_type", ["json", "jsonb"])
async def test_str_field_gets_json_as_raw_text(
    conn: fastpg.Connection,
    json_type: str,
    strategy: fastpg.Strategy,
) -> None:
    sql = f"""SELECT '{{"k": 1}}'::{json_type} AS doc, NULL::{json_type} AS meta"""
    got = await conn.select_one(sql, into=JSONAsTextModel, strategy=strategy)
    assert got == JSONAsTextModel(doc='{"k": 1}', meta=None)


@pytest.mark.parametrize("strategy", ["direct", "json"])
@pytest.mark.parametrize("json_type", ["json", "jsonb"])
async def test_str_field_gets_json_as_raw_text_when_not_null(
    conn: fastpg.Connection,
    json_type: str,
    strategy: fastpg.Strategy,
) -> None:
    sql = f"""SELECT '[1]'::{json_type} AS doc, '"a"'::{json_type} AS meta"""
    got = await conn.select_one(sql, into=JSONAsTextModel, strategy=strategy)
    assert got == JSONAsTextModel(doc="[1]", meta='"a"')


@pytest.mark.parametrize("strategy", ["direct", "json"])
@pytest.mark.parametrize("json_type", ["json", "jsonb"])
async def test_dict_and_any_fields_get_decoded_json(
    conn: fastpg.Connection,
    json_type: str,
    strategy: fastpg.Strategy,
) -> None:
    sql = f"""SELECT '{{"k": 1}}'::{json_type} AS doc, '[1, "a"]'::{json_type} AS meta"""
    got = await conn.select_one(sql, into=JSONDecodedModel, strategy=strategy)
    assert got == JSONDecodedModel(doc={"k": 1}, meta=[1, "a"])


async def test_msgpack_not_offered_for_pydantic(conn: fastpg.Connection) -> None:
    with pytest.raises(fastpg.UnsupportedTypeError, match="msgpack"):
        await conn.select_one(
            "SELECT * FROM people WHERE id = $1",
            1,
            into=PersonModel,
            strategy="msgpack",
        )


async def test_json_strategy_for_msgspec(conn: fastpg.Connection) -> None:
    sql = "SELECT id, name, balance, external_id, created_at, birthday FROM people WHERE id <= 300"
    direct = await conn.select_many(sql, into=PersonStruct, strategy="direct")
    assert direct == await conn.select_many(sql, into=PersonStruct, strategy="json")
