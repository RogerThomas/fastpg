"""select_and_group: rows grouped into Structs whose list fields collect one value per row."""

import uuid
from typing import Annotated, Any, NamedTuple, assert_type

import msgspec
import pytest

import fastpg

CUSTOMERS = """
SELECT * FROM (VALUES ('c1', 'Ann', 'ann@x'), ('c1', 'Annie', 'annie@x'), ('c2', 'Bob', 'bob@x'))
    AS t(customer_id, names, emails)
"""

# Orders 1 and 3 have items; order 2 has none (the LEFT JOIN gives one all-NULL row).
ORDERS = """
SELECT o.id, o.customer, i.sku AS items__sku, i.qty AS items__qty
FROM (VALUES (1, 'ann'), (2, 'bob'), (3, 'ann')) AS o(id, customer)
LEFT JOIN (VALUES (1, 1, 'pen', 2), (2, 3, 'ink', 1), (3, 1, 'pad', 5))
    AS i(line, order_id, sku, qty) ON i.order_id = o.id
ORDER BY o.id, i.line
"""


class Customer(msgspec.Struct):
    customer_id: str
    names: list[str]
    emails: list[str]


class OrderItem(msgspec.Struct):
    sku: str
    qty: int


class Order(msgspec.Struct):
    id: int
    customer: str
    items: list[OrderItem]


async def test_groups_rows_by_the_fields_that_are_not_lists(conn: fastpg.Connection) -> None:
    customers = await conn.select_and_group(CUSTOMERS, into=Customer)
    assert_type(customers, list[Customer])
    assert customers == [
        Customer("c1", ["Ann", "Annie"], ["ann@x", "annie@x"]),
        Customer("c2", ["Bob"], ["bob@x"]),
    ]


@pytest.mark.parametrize("strategy", ["auto", "direct", "msgpack", "json"])
async def test_every_strategy(conn: fastpg.Connection, strategy: fastpg.Strategy) -> None:
    expected = await conn.select_and_group(CUSTOMERS, into=Customer)
    assert await conn.select_and_group(CUSTOMERS, into=Customer, strategy=strategy) == expected
    orders = await conn.select_and_group(ORDERS, into=Order, strategy=strategy)
    assert orders == await conn.select_and_group(ORDERS, into=Order)


async def test_empty_result(conn: fastpg.Connection) -> None:
    assert await conn.select_and_group(f"{CUSTOMERS} WHERE false", into=Customer) == []


class Bag(msgspec.Struct):
    key: int
    values: list[int]


async def test_order_of_groups_and_elements(conn: fastpg.Connection) -> None:
    # Groups come in the order of their first rows; every value is kept, in row order.
    query = """
SELECT key, value AS values
FROM (VALUES (1, 2, 30), (2, 1, 10), (3, 2, 30), (4, 1, 5), (5, 3, 1), (6, 2, 20))
    AS t(n, key, value)
ORDER BY n
"""
    assert await conn.select_and_group(query, into=Bag) == [
        Bag(2, [30, 30, 20]),
        Bag(1, [10, 5]),
        Bag(3, [1]),
    ]


async def test_groups_follow_order_by(conn: fastpg.Connection) -> None:
    query = "SELECT age AS key, id AS values FROM people WHERE id <= $1 ORDER BY age DESC, id"
    bags = await conn.select_and_group(query, 120, into=Bag)
    assert [bag.key for bag in bags] == list(range(77, 17, -1))
    assert bags[17] == Bag(60, [42, 102])
    assert sum(len(bag.values) for bag in bags) == 120


class Cell(NamedTuple):
    a: int
    b: str


class Key(msgspec.Struct, frozen=True):
    n: int


class Mixed(msgspec.Struct):
    id: uuid.UUID
    label: str
    cell: Cell
    key: Key
    xs: list[int]


async def test_hashable_keys_of_any_type(conn: fastpg.Connection) -> None:
    # Key values only need to be hashable: a jsonb NamedTuple or frozen Struct is too.
    query = """
SELECT md5(n::text)::uuid AS id, 'x' AS label, '[1, "b"]'::jsonb AS cell,
       '{"n": 2}'::jsonb AS key, n AS xs
FROM generate_series(1, 3) AS s(n), generate_series(1, 2) AS t(m)
ORDER BY n, m
"""
    groups = await conn.select_and_group(query, into=Mixed)
    assert [g.xs for g in groups] == [[1, 1], [2, 2], [3, 3]]
    assert groups[0].cell == Cell(1, "b")
    assert groups[0].key == Key(2)


class Tags(msgspec.Struct):
    tags: list[str]


async def test_only_list_fields_is_one_group(conn: fastpg.Connection) -> None:
    query = "SELECT name AS tags FROM people WHERE id <= 3 ORDER BY id"
    assert await conn.select_and_group(query, into=Tags) == [
        Tags(["person-1", "person-2", "person-3"]),
    ]


# --- NULLs ----------------------------------------------------------------------------------


class Emails(msgspec.Struct):
    age: int
    emails: list[str | None]


class StrictEmails(msgspec.Struct):
    age: int
    emails: list[str]


async def test_null_goes_into_an_optional_item_type(conn: fastpg.Connection) -> None:
    query = "SELECT age, email AS emails FROM people WHERE age = 60 AND id <= 110 ORDER BY id"
    assert await conn.select_and_group(query, into=Emails) == [
        Emails(60, [None, "person102@example.com"]),
    ]


async def test_null_into_a_non_optional_item_type(conn: fastpg.Connection) -> None:
    query = "SELECT age, email AS emails FROM people WHERE age = 60 AND id <= 110 ORDER BY id"
    with pytest.raises(fastpg.NullValueError, match=r'"emails" is NULL but StrictEmails\.emails'):
        await conn.select_and_group(query, into=StrictEmails)


class Arrays(msgspec.Struct):
    id: int
    tag_lists: list[list[str | None] | None]


async def test_list_of_array_columns(conn: fastpg.Connection) -> None:
    query = """
SELECT 1 AS id, tags AS tag_lists FROM (VALUES (ARRAY['a', NULL]), (NULL), ('{}'))
    AS t(tags)
"""
    assert await conn.select_and_group(query, into=Arrays) == [
        Arrays(1, [["a", None], None, []]),
    ]


# --- Nested Structs -------------------------------------------------------------------------


async def test_nested_structs_and_a_left_join_without_a_match(conn: fastpg.Connection) -> None:
    assert await conn.select_and_group(ORDERS, into=Order) == [
        Order(1, "ann", [OrderItem("pen", 2), OrderItem("pad", 5)]),
        Order(2, "bob", []),
        Order(3, "ann", [OrderItem("ink", 1)]),
    ]


async def test_nested_struct_with_a_null_in_a_non_optional_field(
    conn: fastpg.Connection,
) -> None:
    query = "SELECT 1 AS id, 'ann' AS customer, 'pen' AS items__sku, NULL::int AS items__qty"
    with pytest.raises(fastpg.NullValueError, match=r'"items__qty" is NULL but OrderItem\.qty'):
        await conn.select_and_group(query, into=Order)


class Line(msgspec.Struct):
    sku: str
    note: str | None
    tags: list[str]
    qty: int = 1


class Basket(msgspec.Struct):
    id: int
    lines: list[Line]
    names: list[str]


async def test_nested_struct_fields(conn: fastpg.Connection) -> None:
    # A nested list field taking an array column, an optional one, and a default; next to a
    # flat list field.
    query = """
SELECT 7 AS id, sku AS lines__sku, note AS lines__note, tags AS lines__tags, sku AS names
FROM (VALUES ('a', NULL, ARRAY['x']), ('b', 'fragile', '{}')) AS t(sku, note, tags)
"""
    assert await conn.select_and_group(query, into=Basket) == [
        Basket(7, [Line("a", None, ["x"]), Line("b", "fragile", [])], ["a", "b"]),
    ]
    with_qty = query.replace("AS names", "AS names, 3 AS lines__qty")
    (basket,) = await conn.select_and_group(with_qty, into=Basket)
    assert [line.qty for line in basket.lines] == [3, 3]


# --- Matching columns -----------------------------------------------------------------------


class Contact(msgspec.Struct, rename={"key": "customer_id"}):
    key: str
    names: list[str]
    addresses: list[str] = msgspec.field(name="emails")


class Item(msgspec.Struct, rename="camel"):
    stock_unit: str
    unit_count: int = msgspec.field(name="qty")


class Purchase(msgspec.Struct, rename="camel"):
    order_id: int
    line_items: list[Item]


async def test_renames(conn: fastpg.Connection) -> None:
    assert await conn.select_and_group(CUSTOMERS, into=Contact) == [
        Contact("c1", ["Ann", "Annie"], ["ann@x", "annie@x"]),
        Contact("c2", ["Bob"], ["bob@x"]),
    ]
    # Nested columns are named after the list field's column and the item field's column.
    query = """SELECT 5 AS "orderId", 'pen' AS "lineItems__stockUnit", 2 AS "lineItems__qty" """
    assert await conn.select_and_group(query, into=Purchase) == [
        Purchase(5, [Item("pen", 2)]),
    ]


class Defaults(msgspec.Struct):
    id: int
    tags: list[str] = msgspec.field(default_factory=lambda: ["none"])
    items: list[OrderItem] = []
    region: str = "eu"


async def test_defaults_apply_when_columns_are_left_out(conn: fastpg.Connection) -> None:
    query = "SELECT n AS id FROM generate_series(1, 2) AS s(n)"
    assert await conn.select_and_group(query, into=Defaults) == [
        Defaults(1, ["none"], [], "eu"),
        Defaults(2, ["none"], [], "eu"),
    ]
    query = "SELECT 1 AS id, 'a' AS tags, 'us' AS region, 'pen' AS items__sku, 2 AS items__qty"
    assert await conn.select_and_group(query, into=Defaults) == [
        Defaults(1, ["a"], [OrderItem("pen", 2)], "us"),
    ]


class KwItem(msgspec.Struct, kw_only=True):
    qty: int
    sku: str


class KwOrder(msgspec.Struct, kw_only=True):
    items: list[KwItem]
    id: int
    total: int = 0

    def __post_init__(self) -> None:
        self.total = sum(item.qty for item in self.items)


async def test_keyword_only_fields_and_post_init(conn: fastpg.Connection) -> None:
    assert await conn.select_and_group(ORDERS, into=KwOrder) == [
        KwOrder(id=1, items=[KwItem(sku="pen", qty=2), KwItem(sku="pad", qty=5)]),
        KwOrder(id=2, items=[]),
        KwOrder(id=3, items=[KwItem(sku="ink", qty=1)]),
    ]
    (first, *_) = await conn.select_and_group(ORDERS, into=KwOrder)
    assert first.total == 7


class Adult(msgspec.Struct):
    age: Annotated[int, msgspec.Meta(ge=18)]
    names: list[Annotated[str, msgspec.Meta(max_length=10)]]


async def test_fields_needing_validation(conn: fastpg.Connection) -> None:
    query = "SELECT age, name AS names FROM people WHERE id IN (1, 61) ORDER BY id"
    assert await conn.select_and_group(query, into=Adult) == [Adult(19, ["person-1", "person-61"])]
    with pytest.raises(fastpg.DecodeError, match="length <= 10"):
        await conn.select_and_group("SELECT 20 AS age, 'a long name' AS names", into=Adult)
    with pytest.raises(fastpg.UnsupportedTypeError, match=r"Adult\.age"):
        await conn.select_and_group(query, into=Adult, strategy="direct")


async def test_missing_columns(conn: fastpg.Connection) -> None:
    with pytest.raises(fastpg.MissingColumnError, match=r"Customer requires field\(s\) emails"):
        await conn.select_and_group("SELECT 'c1' AS customer_id, 'Ann' AS names", into=Customer)
    with pytest.raises(fastpg.MissingColumnError, match="customer_id"):
        await conn.select_and_group("SELECT 'Ann' AS names, 'a' AS emails", into=Customer)
    with pytest.raises(fastpg.MissingColumnError, match="items__qty"):
        await conn.select_and_group(
            "SELECT 1 AS id, 'ann' AS customer, 'pen' AS items__sku",
            into=Order,
        )
    with pytest.raises(fastpg.MissingColumnError, match=r'items__qty \(column "items__qty"\)'):
        await conn.select_and_group("SELECT 1 AS id, 'pen' AS items__sku", into=Defaults)


class Strict(msgspec.Struct, forbid_unknown_fields=True):
    customer_id: str
    names: list[str]


async def test_wrong_columns(conn: fastpg.Connection) -> None:
    with pytest.raises(fastpg.UnknownColumnError, match="emails"):
        await conn.select_and_group(CUSTOMERS, into=Strict)
    with pytest.raises(fastpg.TypeMismatchError, match="names"):
        await conn.select_and_group("SELECT 'c1' AS customer_id, 1 AS names", into=Strict)


# --- Query forms and where it runs ----------------------------------------------------------


class Named(msgspec.Struct):
    age: int


async def test_named_params(conn: fastpg.Connection) -> None:
    query = "SELECT age AS key, id AS values FROM people WHERE age = :age AND id <= :id ORDER BY id"
    expected = [Bag(60, [42, 102])]
    assert await conn.select_and_group(query, params={"age": 60, "id": 110}, into=Bag) == expected
    query = "SELECT age AS key, id AS values FROM people WHERE age = :age AND id <= 110 ORDER BY id"
    assert await conn.select_and_group(query, params=Named(60), into=Bag) == expected


async def test_transaction(conn: fastpg.Connection) -> None:
    async with conn.transaction() as tx:
        await tx.execute_script(
            "CREATE TEMP TABLE grouped_tx (k int, v text);"
            "INSERT INTO grouped_tx VALUES (1, 'a'), (2, 'b'), (1, 'c')",
        )
        rows = await tx.select_and_group(
            "SELECT k::text AS customer_id, v AS names, v || '@x' AS emails FROM grouped_tx"
            " WHERE k > $1 ORDER BY v",
            0,
            into=Customer,
            strategy="msgpack",
        )
    assert rows == [Customer("1", ["a", "c"], ["a@x", "c@x"]), Customer("2", ["b"], ["b@x"])]


async def test_pool(dsn: str) -> None:
    async with await fastpg.create_pool(dsn, max_size=2) as pool:
        customers = await pool.select_and_group(CUSTOMERS, into=Customer)
        assert_type(customers, list[Customer])
        assert customers == await pool.select_and_group(CUSTOMERS, into=Customer, strategy="json")
        assert len(customers) == 2
        query = f"SELECT * FROM ({ORDERS}) AS o WHERE id >= $1 ORDER BY id"  # noqa: S608 - constant text
        orders = await pool.select_and_group(query, 2, into=Order)
        assert orders == [Order(2, "bob", []), Order(3, "ann", [OrderItem("ink", 1)])]
        query = "SELECT age AS key, id AS values FROM people WHERE age = :age AND id <= 110"
        query += " ORDER BY id"
        assert await pool.select_and_group(query, params={"age": 60}, into=Bag) == [
            Bag(60, [42, 102]),
        ]
        async with pool.transaction() as tx:
            assert await tx.select_and_group(CUSTOMERS, into=Customer) == customers
        async with pool.acquire() as conn:
            assert await conn.select_and_group(CUSTOMERS, into=Customer) == customers


# --- Errors ---------------------------------------------------------------------------------


class Flat(msgspec.Struct):
    id: int
    name: str


class Point(NamedTuple):
    x: int
    tags: list[str]


@pytest.mark.parametrize(
    "into",
    [dict, Point, int, "Customer"],
    ids=["dict", "tuple", "int", "str"],
)
async def test_into_must_be_a_struct(conn: fastpg.Connection, into: Any) -> None:  # noqa: ANN401
    # Raised before the query runs: this SQL would be a syntax error.
    with pytest.raises(TypeError, match=r"a msgspec\.Struct subclass .*use select_many"):
        await conn.select_and_group("SELEC 1", into=into)


async def test_into_must_have_a_list_field(conn: fastpg.Connection) -> None:
    with pytest.raises(TypeError, match="Flat has none; use select_many"):
        await conn.select_and_group("SELEC 1", into=Flat)


class Document(msgspec.Struct):
    id: int
    meta: dict[str, Any]
    names: list[str]


async def test_unhashable_key_values(conn: fastpg.Connection) -> None:
    query = """SELECT 1 AS id, '{"a": 1}'::jsonb AS meta, 'x' AS names"""
    with pytest.raises(TypeError, match=r"Document\.meta is a dict"):
        await conn.select_and_group(query, into=Document)


class Box(msgspec.Struct):
    label: str
    orders: list[Order]


async def test_nested_grouping_is_not_supported(conn: fastpg.Connection) -> None:
    with pytest.raises(TypeError, match=r"Box\.orders: .*Order.*items.*nested grouping"):
        await conn.select_and_group("SELEC 1", into=Box)


class Clash(msgspec.Struct):
    items__sku: str
    items: list[OrderItem]


async def test_clashing_field_names(conn: fastpg.Connection) -> None:
    with pytest.raises(TypeError, match="items__sku"):
        await conn.select_and_group("SELEC 1", into=Clash)
