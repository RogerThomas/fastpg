"""Struct targets: column binding, type checks, NULLs, strategies."""

import datetime as dt
import gc
import random
import uuid
from decimal import Decimal
from typing import Annotated, Any, Literal, LiteralString, Optional, cast

import msgspec
import pytest

import fastpg
from fastpg._spec import StructPlanner


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


class JSONValue(msgspec.Struct):
    doc: Any


class ConstrainedJSONValue(msgspec.Struct):
    """Constrained, so strategy "auto" reads it through msgpack."""

    id: Annotated[int, msgspec.Meta(ge=0)]
    doc: Any


class ConstrainedJSONDecoded(msgspec.Struct):
    """Constrained, with no Decimal anywhere, so strategy "auto" reads it through msgpack."""

    id: Annotated[int, msgspec.Meta(ge=0)]
    doc: dict[str, int]


class DecimalAmount(msgspec.Struct):
    value: Decimal


class ConstrainedDecimalDict(msgspec.Struct):
    id: Annotated[int, msgspec.Meta(ge=0)]
    doc: dict[str, Decimal]


class ConstrainedDecimalList(msgspec.Struct):
    id: Annotated[int, msgspec.Meta(ge=0)]
    doc: list[Decimal | None]


class ConstrainedDecimalNested(msgspec.Struct):
    id: Annotated[int, msgspec.Meta(ge=0)]
    doc: list[DecimalAmount]


class ConstrainedDecimalAnnotated(msgspec.Struct):
    id: Annotated[int, msgspec.Meta(ge=0)]
    doc: (
        Annotated[
            list[Annotated[Decimal, msgspec.Meta(description="a JSON number")]],
            msgspec.Meta(description="JSON numbers"),
        ]
        | None
    )


class ConstrainedMoney(msgspec.Struct):
    """Constrained, with Decimals only as plain fields (numeric columns, exact through
    msgpack), so strategy "auto" reads it through msgpack."""

    id: Annotated[int, msgspec.Meta(ge=0)]
    amount: Decimal
    refund: Annotated[Decimal, msgspec.Meta(description="money")] | None
    doc: dict[str, int]


type DecimalsInJSON = (
    ConstrainedDecimalDict
    | ConstrainedDecimalList
    | ConstrainedDecimalNested
    | ConstrainedDecimalAnnotated
)

LONG_DECIMAL = "0.1000000000000000055511151231257827"

# Decimals inside a json/jsonb value, and what they must read as (repr: 1.10 == 1.1).
DECIMALS_IN_JSON = [
    (ConstrainedDecimalDict, f'{{"a": 1.10, "b": {LONG_DECIMAL}}}', {
        "a": Decimal("1.10"), "b": Decimal(LONG_DECIMAL),
    }),
    (ConstrainedDecimalList, f"[1.10, null, {LONG_DECIMAL}]", [
        Decimal("1.10"), None, Decimal(LONG_DECIMAL),
    ]),
    (ConstrainedDecimalNested, f'[{{"value": 1.10}}, {{"value": {LONG_DECIMAL}}}]', [
        DecimalAmount(Decimal("1.10")), DecimalAmount(Decimal(LONG_DECIMAL)),
    ]),
    (ConstrainedDecimalAnnotated, f"[1.10, {LONG_DECIMAL}]", [
        Decimal("1.10"), Decimal(LONG_DECIMAL),
    ]),
]  # fmt: skip

# Every way a json/jsonb value reaches a field typed Any: they must all give the same value.
JSON_VALUE_TARGETS = [
    (JSONValue, "direct"),
    (JSONValue, "msgpack"),
    (JSONValue, "json"),
    (ConstrainedJSONValue, "auto"),
]


class RandomJSON:
    """JSON texts made from the edge cases of each JSON token, for differential tests."""

    INTS = (
        "0", "-0", "1", "-1", "9223372036854775807", "-9223372036854775808",
        "9223372036854775808", "-9223372036854775809", "18446744073709551615",
        "18446744073709551616", "98765432109876543210", "-100000000000000000000000000000",
    )  # fmt: skip
    FLOATS = (
        "0.0", "-0.0", "-0e0", "1.5", "-2.25", "1E2", "1e+2", "1e-2", "2.5E-3",
        "0.1000000000000000055511151231257827", "123456789012345678901234567890.5",
        "1.7976931348623157e308", "2.2250738585072014e-308", "5e-324", "1e-400", "-1e-400",
    )  # fmt: skip
    ESCAPES = (
        '\\"', "\\\\", "\\/", "\\b", "\\f", "\\n", "\\r", "\\t", "\\u0001", "\\u001f",
        "\\u007f", "\\u00e9", "\\u00E9", "\\u4e2d", "\\uffff", "\\ud83d\\ude00", "\\uD834\\uDD1E",
    )  # fmt: skip
    CHARS = ("a", "Z", " ", "é", "中", "😀", "ключ")
    KEYS = ('"a"', '"b"', '"aa"', '"z"', '""', '"é"', '"\\u00e9"', '"😀"', '"\\n"')
    SPACES = ("", "", "", " ", "\n", "\t", "\r\n  ")

    def __init__(self, seed: int, *, nul: bool) -> None:
        self.rng = random.Random(seed)  # noqa: S311 - reproducible data, not cryptography
        # \u0000 is valid JSON, but jsonb refuses it.
        self.escapes = (*self.ESCAPES, "\\u0000") if nul else self.ESCAPES

    def ws(self) -> str:
        return self.rng.choice(self.SPACES)

    def string(self) -> str:
        pieces = self.rng.choices((self.CHARS, self.escapes), k=self.rng.randint(0, 6))
        return '"' + "".join(self.rng.choice(p) for p in pieces) + '"'

    def float_text(self) -> str:
        if self.rng.random() < 0.5:
            return self.rng.choice(self.FLOATS)
        exponent = self.rng.randint(-330, 300)
        return f"{self.rng.uniform(-10, 10):.{self.rng.randint(1, 17)}f}e{exponent}"

    def value(self, depth: int) -> str:
        kind = self.rng.choice(("int", "float", "str", "lit", "array", "object"))
        if depth >= 5 and kind in ("array", "object"):
            kind = "str"
        match kind:
            case "int":
                return self.rng.choice((*self.INTS, str(self.rng.randint(-(10**6), 10**6))))
            case "float":
                return self.float_text()
            case "str":
                return self.string()
            case "lit":
                return self.rng.choice(("true", "false", "null"))
            case "array":
                items = [self.value(depth + 1) for _ in range(self.rng.randint(0, 4))]
                return "[" + self.ws() + ("," + self.ws()).join(items) + self.ws() + "]"
            case _:  # keys from a small set, so objects often repeat one
                members = [
                    f"{self.rng.choice((*self.KEYS, self.string()))}{self.ws()}:{self.ws()}"
                    f"{self.value(depth + 1)}"
                    for _ in range(self.rng.randint(0, 5))
                ]
                return "{" + self.ws() + ("," + self.ws()).join(members) + self.ws() + "}"

    def document(self) -> str:
        return self.ws() + self.value(0) + self.ws()


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
    json_type: LiteralString,
    strategy: fastpg.Strategy,
) -> None:
    query = f"""SELECT '{{"k": 1}}'::{json_type} AS doc, NULL::{json_type} AS meta"""
    got = await conn.select_one(query, into=JSONAsText, strategy=strategy)
    assert got == JSONAsText('{"k": 1}', None)


@pytest.mark.parametrize("strategy", ["direct", "msgpack", "json"])
@pytest.mark.parametrize("json_type", ["json", "jsonb"])
async def test_str_field_gets_json_as_raw_text_when_not_null(
    conn: fastpg.Connection,
    json_type: LiteralString,
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
    json_type: LiteralString,
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
    json_type: LiteralString,
    strategy: fastpg.Strategy,
) -> None:
    query = f"""SELECT '{{"k": 1}}'::{json_type} AS doc, '[1, "a"]'::{json_type} AS meta"""
    got = await conn.select_one(query, into=JSONDecoded, strategy=strategy)
    assert got == JSONDecoded({"k": 1}, [1, "a"])


A: LiteralString = "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11"
B: LiteralString = "28dd2c79-55ce-9264-5624-0b2ff0100bde"
UUID_A, UUID_B = uuid.UUID(A), uuid.UUID(B)


class UuidItem(msgspec.Struct):
    id: uuid.UUID


class UuidShapes(msgspec.Struct):
    plain: uuid.UUID
    nullable: uuid.UUID | None
    null: uuid.UUID | None
    optional: Optional[uuid.UUID]  # noqa: UP045 - the spelling under test
    nested: UuidItem
    array: list[uuid.UUID | None]


UUID_SHAPES_QUERY = f"""
SELECT '{A}'::uuid AS plain, '{B}'::uuid AS nullable, NULL::uuid AS null,
       '{A}'::uuid AS optional, '{{"id": "{A}"}}'::jsonb AS nested,
       ARRAY['{A}', NULL]::uuid[] AS array
"""


def test_only_uuid_fields_take_uuids_as_bin() -> None:
    # The columns the msgpack route encodes as 16-byte bin: the rest get the text.
    plan = StructPlanner.plan(UuidShapes)
    assert plan.uuid_columns == ("plain", "nullable", "null", "optional")
    assert StructPlanner.plan(UuidsAsText).uuid_columns == ()
    assert StructPlanner.plan(AnnotatedUuids).uuid_columns == ("described", "described_or_null")


@pytest.mark.parametrize("strategy", ["msgpack", "json"])
async def test_uuid_fields_match_direct_construction(
    conn: fastpg.Connection,
    strategy: fastpg.Strategy,
) -> None:
    direct = await conn.select_one(UUID_SHAPES_QUERY, into=UuidShapes, strategy="direct")
    got = await conn.select_one(UUID_SHAPES_QUERY, into=UuidShapes, strategy=strategy)
    assert got == direct
    assert direct == UuidShapes(
        plain=UUID_A,
        nullable=UUID_B,
        null=None,
        optional=UUID_A,
        nested=UuidItem(UUID_A),
        array=[UUID_A, None],
    )
    assert type(got.plain) is uuid.UUID
    assert type(got.optional) is uuid.UUID
    assert type(got.nested.id) is uuid.UUID


@pytest.mark.parametrize("strategy", ["msgpack", "json"])
async def test_uuid_fields_match_direct_construction_across_rows(
    conn: fastpg.Connection,
    strategy: fastpg.Strategy,
) -> None:
    query = """
SELECT md5(n::text)::uuid AS plain, CASE WHEN n % 2 = 0 THEN md5(n::text)::uuid END AS nullable,
       NULL::uuid AS null, md5(n::text)::uuid AS optional,
       json_build_object('id', md5(n::text)::uuid) AS nested, ARRAY[md5(n::text)::uuid] AS array
FROM generate_series(1, 50) AS s(n)
"""
    direct = await conn.select_many(query, into=UuidShapes, strategy="direct")
    assert await conn.select_many(query, into=UuidShapes, strategy=strategy) == direct
    assert sum(row.nullable is None for row in direct) == 25


class UuidsAsText(msgspec.Struct):
    text: str
    text_or_null: str | None
    anything: Any


@pytest.mark.parametrize("strategy", ["msgpack", "json"])
async def test_uuid_columns_keep_their_text_for_other_fields(
    conn: fastpg.Connection,
    strategy: fastpg.Strategy,
) -> None:
    # Direct construction refuses a uuid column for a `str` field; these routes give the text.
    query = f"SELECT '{A}'::uuid AS text, NULL::uuid AS text_or_null, '{B}'::uuid AS anything"
    got = await conn.select_one(query, into=UuidsAsText, strategy=strategy)
    assert got == UuidsAsText(str(UUID_A), None, str(UUID_B))
    query = f"SELECT '{A}'::uuid AS text, '{B}'::uuid AS text_or_null, NULL AS anything"
    got = await conn.select_one(query, into=UuidsAsText, strategy=strategy)
    assert got == UuidsAsText(str(UUID_A), str(UUID_B), None)


class AnnotatedUuids(msgspec.Struct):
    described: Annotated[uuid.UUID, msgspec.Meta(description="an id")]
    described_or_null: Annotated[uuid.UUID | None, msgspec.Meta(title="maybe")]
    text: Annotated[str, msgspec.Meta(description="an id as text")]


@pytest.mark.parametrize("strategy", ["auto", "msgpack", "json"])
async def test_annotated_uuid_fields(conn: fastpg.Connection, strategy: fastpg.Strategy) -> None:
    # `Annotated` fields need msgspec validation: direct construction doesn't take them.
    query = f"""
SELECT '{A}'::uuid AS described, NULL::uuid AS described_or_null, '{B}'::uuid AS text
"""
    got = await conn.select_one(query, into=AnnotatedUuids, strategy=strategy)
    assert got == AnnotatedUuids(UUID_A, None, str(UUID_B))
    assert type(got.described) is uuid.UUID
    query = f"SELECT '{A}'::uuid AS described, '{B}'::uuid AS described_or_null, 'x' AS text"
    got = await conn.select_one(query, into=AnnotatedUuids, strategy=strategy)
    assert got == AnnotatedUuids(UUID_A, UUID_B, "x")


@pytest.mark.parametrize(("into", "strategy"), JSON_VALUE_TARGETS)
@pytest.mark.parametrize("json_type", ["json", "jsonb"])
async def test_json_integers_beyond_64_bits_keep_every_digit(
    conn: fastpg.Connection,
    json_type: LiteralString,
    into: type[JSONValue | ConstrainedJSONValue],
    strategy: fastpg.Strategy,
) -> None:
    numbers = [2**64, 98765432109876543210, -(2**63) - 1, -(10**30), 2**64 - 1, -(2**63)]
    query = cast("LiteralString", f"SELECT 1 AS id, '{numbers}'::{json_type} AS doc")
    got = await conn.select_one(query, into=into, strategy=strategy)
    assert got.doc == numbers
    assert all(type(n) is int for n in got.doc)


@pytest.mark.parametrize(("into", "strategy"), JSON_VALUE_TARGETS)
@pytest.mark.parametrize(
    ("json_type", "keys", "inner_keys"),
    [
        ("json", ["b", "aa", "c"], ["y", "x"]),  # as written
        ("jsonb", ["b", "c", "aa"], ["x", "y"]),  # the server's order: shorter keys first
    ],
)
async def test_json_object_keys_keep_the_servers_order(
    conn: fastpg.Connection,
    json_type: LiteralString,
    keys: list[str],
    inner_keys: list[str],
    into: type[JSONValue | ConstrainedJSONValue],
    strategy: fastpg.Strategy,
) -> None:
    query = (
        f"""SELECT 1 AS id, '{{"b": 1, "aa": 2, "c": {{"y": 3, "x": 4}}}}'::{json_type} AS doc"""
    )
    got = await conn.select_one(query, into=into, strategy=strategy)
    assert got.doc == {"b": 1, "aa": 2, "c": {"y": 3, "x": 4}}
    assert list(got.doc) == keys
    assert list(got.doc["c"]) == inner_keys


@pytest.mark.parametrize(("into", "strategy"), JSON_VALUE_TARGETS)
@pytest.mark.parametrize("json_type", ["json", "jsonb"])
async def test_deeply_nested_json(
    conn: fastpg.Connection,
    json_type: LiteralString,
    into: type[JSONValue | ConstrainedJSONValue],
    strategy: fastpg.Strategy,
) -> None:
    depth = 1000
    query = f"SELECT 1 AS id, '{'[' * depth}{{}}{']' * depth}'::{json_type} AS doc"
    got = await conn.select_one(query, into=into, strategy=strategy)
    expected: object = {}
    for _ in range(depth):
        expected = [expected]
    assert got.doc == expected


@pytest.mark.parametrize("json_type", ["json", "jsonb"])
async def test_json_integer_beyond_64_bits_in_an_int_field(
    conn: fastpg.Connection,
    json_type: LiteralString,
) -> None:
    query = f"""SELECT '{{"k": 98765432109876543210}}'::{json_type} AS doc, NULL AS meta"""
    for strategy in ("direct", "json"):
        got = await conn.select_one(query, into=JSONDecoded, strategy=strategy)
        assert got == JSONDecoded({"k": 98765432109876543210}, None)
    # MessagePack integers stop at 64 bits: a typed field can't take it through msgpack.
    with pytest.raises(fastpg.DecodeError, match="beyond 64 bits") as exc:
        await conn.select_one(query, into=JSONDecoded, strategy="msgpack")
    assert isinstance(exc.value.__cause__, msgspec.ValidationError)


@pytest.mark.parametrize(("into", "text", "expected"), DECIMALS_IN_JSON)
@pytest.mark.parametrize("json_type", ["json", "jsonb"])
async def test_auto_keeps_decimal_digits_inside_json(
    conn: fastpg.Connection,
    json_type: LiteralString,
    into: type[DecimalsInJSON],
    text: LiteralString,
    expected: object,
) -> None:
    # MessagePack has no decimal type: "auto" reads these through JSON, not msgpack.
    query = f"SELECT 1 AS id, '{text}'::{json_type} AS doc"
    auto = await conn.select_one(query, into=into)
    json = await conn.select_one(query, into=into, strategy="json")
    assert repr(json.doc) == repr(expected)
    assert repr(auto) == repr(json)


@pytest.mark.parametrize("json_type", ["json", "jsonb"])
async def test_auto_reads_json_without_decimals_through_msgpack(
    conn: fastpg.Connection,
    json_type: LiteralString,
) -> None:
    # MessagePack integers stop at 64 bits, so this error shows the route "auto" took.
    query = f"""SELECT 1 AS id, '{{"k": 98765432109876543210}}'::{json_type} AS doc"""
    with pytest.raises(fastpg.DecodeError, match="beyond 64 bits"):
        await conn.select_one(query, into=ConstrainedJSONDecoded)


async def test_auto_reads_plain_decimal_fields_through_msgpack(conn: fastpg.Connection) -> None:
    query = "SELECT 1 AS id, 1.10 AS amount, NULL::numeric AS refund, $1::jsonb AS doc"
    got = await conn.select_one(query, '{"k": 1}', into=ConstrainedMoney)
    assert repr(got) == repr(ConstrainedMoney(1, Decimal("1.10"), None, {"k": 1}))
    # MessagePack integers stop at 64 bits, so this error shows the route "auto" took.
    with pytest.raises(fastpg.DecodeError, match="beyond 64 bits"):
        await conn.select_one(query, '{"k": 98765432109876543210}', into=ConstrainedMoney)


@pytest.mark.parametrize("json_type", ["json", "jsonb"])
async def test_random_json_documents_read_the_same_through_every_strategy(
    conn: fastpg.Connection,
    json_type: LiteralString,
) -> None:
    generator = RandomJSON(20261108, nul=json_type == "json")
    documents = [generator.document() for _ in range(400)]
    source = "unnest($1::text[]) WITH ORDINALITY AS t(d, n) ORDER BY n"
    query = f"SELECT d::{json_type} AS doc FROM {source}"  # noqa: S608 - fixed text
    results: dict[str, list[str]] = {}
    for strategy in ("direct", "msgpack", "json"):
        rows = await conn.select_many(query, documents, into=JSONValue, strategy=strategy)
        # repr tells apart what == doesn't: key order, 1 and 1.0, 0.0 and -0.0.
        results[strategy] = [repr(row.doc) for row in rows]
    assert len(results["direct"]) == len(documents)
    for strategy in ("msgpack", "json"):
        mismatches = [
            (text, direct, other)
            for text, direct, other in zip(
                documents,
                results["direct"],
                results[strategy],
                strict=True,
            )
            if direct != other
        ]
        assert mismatches == [], strategy


@pytest.mark.parametrize("text", ['"\\ud800"', '"a\\udc00"', '["\\ud800", 1]'])
async def test_json_with_a_lone_surrogate_escape(
    conn: fastpg.Connection,
    text: LiteralString,
) -> None:
    # The json type keeps such text (jsonb refuses it); no strategy can decode it.
    query = f"SELECT '{text}'::json AS doc"
    for strategy in ("direct", "msgpack", "json"):
        with pytest.raises(fastpg.DecodeError):
            await conn.select_one(query, into=JSONValue, strategy=strategy)


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
    extra_columns: LiteralString,
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
