"""Struct targets: column binding, type checks, NULLs, strategies."""

import datetime as dt
import gc
import uuid
from decimal import Decimal
from typing import Annotated, Any, Literal

import msgspec
import pytest

import fastpg


class Person(msgspec.Struct):
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


class NameAge(msgspec.Struct):
    name: str
    age: int


class StrictName(msgspec.Struct, forbid_unknown_fields=True):
    name: str


class WithDefaults(msgspec.Struct):
    name: str
    tags: list[str] = msgspec.field(default_factory=list[str])
    nickname: str = "anon"


class Wrong(msgspec.Struct):
    name: int


class NotNullEmail(msgspec.Struct):
    email: str


class Score(msgspec.Struct):
    score: float


class Camel(msgspec.Struct, rename="camel"):
    external_id: uuid.UUID
    created_at: dt.datetime


class KwOnlyBase(msgspec.Struct):
    name: str


class WithKwOnly(KwOnlyBase, kw_only=True):
    age: int


class Upper(msgspec.Struct):
    name: str

    def __post_init__(self) -> None:
        self.name = self.name.upper()


class Point(msgspec.Struct):
    x: int
    y: int


class PointsDoc(msgspec.Struct):
    points: list[Point]
    meta: dict[str, int] | None


class IntsDoc(msgspec.Struct):
    points: list[int]


class Constrained(msgspec.Struct):
    age: Annotated[int, msgspec.Meta(ge=50)]
    kind: Literal["person"] = "person"


class Pair(msgspec.Struct, array_like=True):
    name: str
    age: int


class IdEmail(msgspec.Struct):
    id: int
    email: str


class Scalars(msgspec.Struct):
    id: int
    external_id: uuid.UUID
    balance: Decimal
    email: str | None


class ScalarsPostInit(Scalars):
    def __post_init__(self) -> None:
        self.email = self.email or "email"


class ScalarsWithDict(Scalars, dict=True):
    pass


class ScalarsWithDefault(Scalars):
    tags: list[str] = msgspec.field(default_factory=list[str])


class ScalarsAndJSON(Scalars):
    meta: dict[str, int] | None


class JSONAsText(msgspec.Struct):
    doc: str
    meta: str | None


class JSONDecoded(msgspec.Struct):
    doc: dict[str, int]
    meta: Any


class ConstrainedJSONAsText(msgspec.Struct):
    doc: Annotated[str, msgspec.Meta(max_length=8)]


class InternalChar(msgspec.Struct):
    kind: str
    empty: str
    other: str | None


async def test_select_one_into_struct(conn: fastpg.Connection) -> None:
    person = await conn.select_one("SELECT * FROM people WHERE id = $1", 77, into=Person)
    assert person == Person(
        id=77,
        name="person-77",
        age=35,
        email=None,
        score=7.7,
        balance=Decimal("28.49"),
        active=False,
        external_id=uuid.UUID("28dd2c79-55ce-9264-5624-0b2ff0100bde"),
        created_at=dt.datetime(2024, 1, 1, 1, 18, 17, tzinfo=dt.UTC),
        birthday=None,
    )


@pytest.mark.parametrize("strategy", ["direct", "msgpack"])
async def test_strategies_agree_on_seed_data(
    conn: fastpg.Connection,
    strategy: fastpg.Strategy,
) -> None:
    query = "SELECT * FROM people WHERE id BETWEEN $1 AND $2 ORDER BY id"
    expected = await conn.select_many(query, 1, 3000, into=Person, strategy="direct")
    got = await conn.select_many(query, 1, 3000, into=Person, strategy=strategy)
    assert len(got) == 3000
    assert got == expected
    assert [p.id for p in got] == list(range(1, 3001))


async def test_column_order_independent(conn: fastpg.Connection) -> None:
    in_order = await conn.select_one("SELECT name, age FROM people WHERE id = 3", into=NameAge)
    reversed_ = await conn.select_one("SELECT age, name FROM people WHERE id = 3", into=NameAge)
    assert in_order == reversed_ == NameAge("person-3", 21)


async def test_extra_columns_ignored_by_default(conn: fastpg.Connection) -> None:
    got = await conn.select_one(
        "SELECT id, name, age, email FROM people WHERE id = 3",
        into=NameAge,
    )
    assert got == NameAge("person-3", 21)


async def test_extra_columns_rejected_with_forbid_unknown_fields(conn: fastpg.Connection) -> None:
    with pytest.raises(fastpg.UnknownColumnError, match=r'"age" .*\(forbid_unknown_fields=True\)$'):
        await conn.select_one("SELECT name, age FROM people WHERE id = 3", into=StrictName)


async def test_missing_required_field(conn: fastpg.Connection) -> None:
    with pytest.raises(fastpg.MissingColumnError, match="age"):
        await conn.select_one("SELECT name FROM people WHERE id = 3", into=NameAge)


async def test_missing_field_with_default_uses_default(conn: fastpg.Connection) -> None:
    got = await conn.select_one("SELECT name FROM people WHERE id = 3", into=WithDefaults)
    assert got == WithDefaults("person-3", [], "anon")


async def test_type_mismatch_detected_before_decoding(conn: fastpg.Connection) -> None:
    expected = r'"name" has PostgreSQL type text.*Wrong\.name expects int'
    with pytest.raises(fastpg.TypeMismatchError, match=expected):
        await conn.select_one("SELECT name FROM people WHERE id = 3", into=Wrong)


async def test_type_mismatch_raised_even_for_empty_results(conn: fastpg.Connection) -> None:
    with pytest.raises(fastpg.TypeMismatchError):
        await conn.select_many("SELECT name FROM people WHERE false", into=Wrong)


async def test_null_into_non_optional(conn: fastpg.Connection) -> None:
    with pytest.raises(fastpg.NullValueError, match=r'"email" is NULL but NotNullEmail\.email'):
        await conn.select_one("SELECT email FROM people WHERE id = 7", into=NotNullEmail)


async def test_null_into_non_optional_via_msgpack_is_decode_error(conn: fastpg.Connection) -> None:
    with pytest.raises(fastpg.DecodeError, match="email"):
        await conn.select_one(
            "SELECT email FROM people WHERE id = 7",
            into=NotNullEmail,
            strategy="msgpack",
        )


async def test_int_column_into_float_field(conn: fastpg.Connection) -> None:
    got = await conn.select_one("SELECT 5::int4 AS score", into=Score)
    assert got.score == 5.0
    assert type(got.score) is float


async def test_renamed_fields_match_encoded_names(conn: fastpg.Connection) -> None:
    query = 'SELECT external_id AS "externalId", created_at AS "createdAt" FROM people WHERE id = 1'
    direct = await conn.select_one(query, into=Camel, strategy="direct")
    assert direct == await conn.select_one(query, into=Camel, strategy="msgpack")


async def test_kw_only_fields(conn: fastpg.Connection) -> None:
    query = "SELECT name, age FROM people WHERE id = 3"
    assert await conn.select_one(query, into=WithKwOnly) == WithKwOnly(name="person-3", age=21)


async def test_post_init_runs(conn: fastpg.Connection) -> None:
    got = await conn.select_one("SELECT name FROM people WHERE id = 3", into=Upper)
    assert got.name == "PERSON-3"


async def test_json_fields_decode_with_field_type(conn: fastpg.Connection) -> None:
    query = """SELECT '[{"x": 1, "y": 2}]'::jsonb AS points, NULL::jsonb AS meta"""
    got = await conn.select_one(query, into=PointsDoc, strategy="direct")
    assert got == PointsDoc([Point(1, 2)], None)
    assert got == await conn.select_one(query, into=PointsDoc, strategy="msgpack")


async def test_invalid_json_shape_is_decode_error(conn: fastpg.Connection) -> None:
    with pytest.raises(fastpg.DecodeError, match="points") as exc:
        await conn.select_one("""SELECT '["a"]'::jsonb AS points""", into=IntsDoc)
    assert isinstance(exc.value.__cause__, msgspec.ValidationError)


@pytest.mark.parametrize("strategy", ["direct", "msgpack", "json"])
@pytest.mark.parametrize("json_type", ["json", "jsonb"])
async def test_str_field_gets_json_as_raw_text(
    conn: fastpg.Connection,
    json_type: str,
    strategy: fastpg.Strategy,
) -> None:
    query = f"""SELECT '{{"k": 1}}'::{json_type} AS doc, NULL::{json_type} AS meta"""
    got = await conn.select_one(query, into=JSONAsText, strategy=strategy)
    assert got == JSONAsText('{"k": 1}', None)


@pytest.mark.parametrize("strategy", ["direct", "msgpack", "json"])
@pytest.mark.parametrize("json_type", ["json", "jsonb"])
async def test_str_field_gets_json_as_raw_text_when_not_null(
    conn: fastpg.Connection,
    json_type: str,
    strategy: fastpg.Strategy,
) -> None:
    query = f"""SELECT '[1]'::{json_type} AS doc, '"a"'::{json_type} AS meta"""
    got = await conn.select_one(query, into=JSONAsText, strategy=strategy)
    assert got == JSONAsText("[1]", '"a"')


@pytest.mark.parametrize("strategy", ["direct", "msgpack", "json"])
async def test_str_fields_get_internal_char(
    conn: fastpg.Connection,
    strategy: fastpg.Strategy,
) -> None:
    query = """SELECT 'r'::"char" AS kind, '\\000'::"char" AS empty, NULL::"char" AS other"""
    got = await conn.select_one(query, into=InternalChar, strategy=strategy)
    assert got == InternalChar("r", "", None)


@pytest.mark.parametrize("json_type", ["json", "jsonb"])
async def test_constrained_str_field_gets_json_as_raw_text(
    conn: fastpg.Connection,
    json_type: str,
) -> None:
    got = await conn.select_one(f"SELECT '[1]'::{json_type} AS doc", into=ConstrainedJSONAsText)
    assert got == ConstrainedJSONAsText("[1]")
    with pytest.raises(fastpg.DecodeError, match="length <= 8"):
        await conn.select_one(
            f"SELECT '[1, 2, 3, 4]'::{json_type} AS doc",
            into=ConstrainedJSONAsText,
        )


@pytest.mark.parametrize("strategy", ["direct", "msgpack", "json"])
@pytest.mark.parametrize("json_type", ["json", "jsonb"])
async def test_dict_and_any_fields_get_decoded_json(
    conn: fastpg.Connection,
    json_type: str,
    strategy: fastpg.Strategy,
) -> None:
    query = f"""SELECT '{{"k": 1}}'::{json_type} AS doc, '[1, "a"]'::{json_type} AS meta"""
    got = await conn.select_one(query, into=JSONDecoded, strategy=strategy)
    assert got == JSONDecoded({"k": 1}, [1, "a"])


async def test_auto_falls_back_to_msgpack_for_constraints(conn: fastpg.Connection) -> None:
    ok = await conn.select_one("SELECT age FROM people WHERE id = $1", 40, into=Constrained)
    assert ok.age == 58
    with pytest.raises(fastpg.DecodeError, match=">= 50"):
        await conn.select_one("SELECT age FROM people WHERE id = $1", 3, into=Constrained)


async def test_direct_refuses_constraints(conn: fastpg.Connection) -> None:
    with pytest.raises(fastpg.UnsupportedTypeError, match=r"Constrained\.age"):
        await conn.select_one("SELECT 60 AS age", into=Constrained, strategy="direct")


async def test_array_like_struct_direct(conn: fastpg.Connection) -> None:
    got = await conn.select_one("SELECT name, age FROM people WHERE id = 3", into=Pair)
    assert got == Pair("person-3", 21)
    with pytest.raises(fastpg.UnsupportedTypeError, match="array_like"):
        await conn.select_one("SELECT 'x' AS name, 1 AS age", into=Pair, strategy="msgpack")


async def test_select_one_or_none_into(conn: fastpg.Connection) -> None:
    query = "SELECT * FROM people WHERE id = $1"
    assert await conn.select_one_or_none(query, -1, into=Person) is None
    assert await conn.select_one_or_none(query, -1, into=Person, strategy="msgpack") is None
    person = await conn.select_one_or_none(query, 5, into=Person)
    assert person is not None
    assert person.id == 5


async def test_into_must_be_struct_or_model(conn: fastpg.Connection) -> None:
    untyped_caller: Any = int  # what an untyped caller could pass
    with pytest.raises(TypeError, match=r"msgspec\.Struct"):
        await conn.select_one("SELECT 1", into=untyped_caller)


async def test_large_result_streams_in_chunks(conn: fastpg.Connection) -> None:
    # Spans several 512-row chunks with a partial final chunk.
    rows = await conn.select_many("SELECT * FROM people WHERE id <= 5000 ORDER BY id", into=Person)
    assert [p.id for p in rows] == list(range(1, 5001))
    tuples = await conn.select_many("SELECT id FROM people WHERE id <= 5000 ORDER BY id")
    assert tuples == [(i,) for i in range(1, 5001)]


async def test_rows_of_scalars_are_not_gc_tracked(conn: fastpg.Connection) -> None:
    # As msgspec does for Structs whose values can't hold references (uuid.UUID and
    # Decimal included), so large results don't make the cyclic GC traverse every row.
    query = "SELECT id, external_id, balance, email FROM people WHERE id <= 3000 ORDER BY id"
    rows = await conn.select_many(query, into=Scalars)
    gc.collect()
    assert not any(gc.is_tracked(row) for row in rows)
    assert rows[6] == Scalars(
        7,
        uuid.UUID("8f14e45f-ceea-167a-5a36-dedd4bea2543"),
        Decimal("2.59"),
        None,
    )
    untyped_caller: Any = rows[0]  # what an untyped caller could do
    untyped_caller.email = [rows[0]]
    assert gc.is_tracked(rows[0])  # msgspec re-tracks a Struct given a container


@pytest.mark.parametrize(
    ("into", "extra_columns"),
    [
        (ScalarsPostInit, ""),
        (ScalarsWithDict, ""),
        (ScalarsWithDefault, ""),
        (ScalarsAndJSON, """, '{"k": 1}'::jsonb AS meta"""),
    ],
)
async def test_rows_that_could_hold_references_stay_gc_tracked(
    conn: fastpg.Connection,
    into: type[Scalars],
    extra_columns: str,
) -> None:
    columns = f"id, external_id, balance, email{extra_columns}"
    rows = await conn.select_many(f"SELECT {columns} FROM people WHERE id <= 3", into=into)  # noqa: S608
    assert [gc.is_tracked(row) for row in rows] == [True, True, True]


async def test_error_in_first_chunk_fails_query(conn: fastpg.Connection) -> None:
    # Every 7th row has a NULL email; id 7 is in the first chunk.
    with pytest.raises(fastpg.NullValueError):
        await conn.select_many("SELECT id, email FROM people ORDER BY id LIMIT 3000", into=IdEmail)
    assert await conn.select_one("SELECT 1") == (1,)


async def test_error_in_later_chunk_fails_query(conn: fastpg.Connection) -> None:
    # Only id 2100 has a NULL email here, ~1800 rows in: after the first 512-row chunks
    # have already been decoded and accumulated.
    query = "SELECT id, email FROM people WHERE id % 7 <> 0 OR id = 2100 ORDER BY id LIMIT 3000"
    with pytest.raises(fastpg.NullValueError):
        await conn.select_many(query, into=IdEmail)
    assert await conn.select_one("SELECT 1") == (1,)
