# Arrays and grouping

Lists go in and out three ways:

* array columns are read into `list[X]` fields;
* Python lists are sent as arrays, e.g. for `= ANY($1)`;
* `select_and_group` collects plain rows into list fields.

## Reading arrays

A `list[X]` field takes an array of values that fit `X`; use `list[X | None]` if the array
can hold `NULL`s. Arrays must have one dimension and start at index 1 (the default).

`array_agg` reads one-to-many data in one query:

```python
import msgspec


class AgeGroup(msgspec.Struct):
    age: int
    names: list[str]
    emails: list[str | None]


query = """
SELECT age, array_agg(name ORDER BY id) AS names, array_agg(email ORDER BY id) AS emails
FROM people WHERE id <= 110 AND age = 60 GROUP BY age
"""
print(await conn.select_many(query, into=AgeGroup))
# [AgeGroup(age=60, names=['person-42', 'person-102'], emails=[None, 'person102@example.com'])]
```

## Sending lists

A list is sent as an array where the query takes an array, and as JSON where it takes
`json` or `jsonb`:

```python
query = "SELECT id FROM people WHERE id = ANY($1) ORDER BY id"
print(await conn.select_many(query, [3, 8, 42]))  # [(3,), (8,), (42,)]
```

## Grouping rows

`select_and_group` reads one-to-many data from plain rows, such as the rows of a join.
Rows with the same values in the Struct's other fields become one Struct, and each list
field collects one value from each of those rows:

```python
import msgspec


class Cohort(msgspec.Struct):
    age: int
    names: list[str]


query = """
SELECT age, name AS names FROM people WHERE id <= 110 AND age IN (60, 61) ORDER BY age, id
"""
print(await conn.select_and_group(query, into=Cohort))
# [Cohort(age=60, names=['person-42', 'person-102']), Cohort(age=61, names=['person-43', 'person-103'])]
```

The Structs come in the order of their first rows, so `ORDER BY` sets the order of both the
Structs and the values in their lists.

A list of Structs collects one Struct per row, from columns named after the list field, two
underscores and the inner field, such as `items__sku`.
A row where those columns are all `NULL` adds nothing, so with a `LEFT JOIN` an order
without items gets an empty list:

```python
import msgspec


class Item(msgspec.Struct):
    sku: str
    qty: int


class Order(msgspec.Struct):
    id: int
    items: list[Item]


await conn.execute_script("""
CREATE TEMP TABLE orders (id int PRIMARY KEY);
CREATE TEMP TABLE order_items (order_id int, sku text, qty int);
INSERT INTO orders VALUES (1), (2);
INSERT INTO order_items VALUES (1, 'pen', 2), (1, 'ink', 1);
""")
query = """
SELECT o.id, i.sku AS items__sku, i.qty AS items__qty
FROM orders o LEFT JOIN order_items i ON i.order_id = o.id
ORDER BY o.id, i.sku
"""
print(await conn.select_and_group(query, into=Order))
# [Order(id=1, items=[Item(sku='ink', qty=1), Item(sku='pen', qty=2)]), Order(id=2, items=[])]
```
