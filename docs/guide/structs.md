# Structs

Pass a [msgspec](https://msgspec.dev/) Struct class as `into=`, and each row becomes an
instance of it.
This is the recommended way to read rows, and the fastest.

```python
import datetime
import decimal

import msgspec


class Person(msgspec.Struct):
    id: int
    name: str
    email: str | None
    balance: decimal.Decimal
    created_at: datetime.datetime


query = "SELECT id, name, email, balance, created_at FROM people WHERE id = $1"
person = await conn.select_one(query, 1, into=Person)
print(person.name, person.balance, person.created_at)
# person-1 0.37 2024-01-01 00:01:01+00:00
```

`select_many(..., into=Person)` returns a `list[Person]`, and type checkers know it.

## How columns match fields

* Each column fills the field with the same name. Column order doesn't matter.
* Use msgspec's `rename=` when a field's name differs from its column.
* A field with a default can be left out of the query.
* Extra columns are ignored.

```python
import msgspec


class Contact(msgspec.Struct, rename={"display_name": "name"}):
    display_name: str
    verified: bool = False


query = "SELECT id, name FROM people WHERE id <= $1 ORDER BY id"
print(await conn.select_many(query, 2, into=Contact))
# [Contact(display_name='person-1', verified=False), Contact(display_name='person-2', verified=False)]
```

fastpg checks the columns against the fields before it reads any rows.
A mismatch raises an error, even when the query returns no rows:

* a required field with no column: `MissingColumnError`;
* a column whose type doesn't fit its field, or the same column name twice:
  `TypeMismatchError`;
* an extra column, if the Struct sets `forbid_unknown_fields=True`: `UnknownColumnError`.

A `NULL` in a field that isn't `| None` raises `NullValueError`.

## Field types

Fields can be `bool`, `int`, `float`, `str`, `bytes`, `datetime`, `date`, `time`,
`uuid.UUID`, `decimal.Decimal` or `Any`, each optionally `| None`.
A `json` or `jsonb` column can also fill a list, a dict or a nested Struct.
See [Types](queries.md#types) for which column fills which field.

## Validation

msgspec checks constraints, enums and `Literal` fields as usual:

```python
from typing import Annotated

import msgspec


class Adult(msgspec.Struct):
    name: str
    age: Annotated[int, msgspec.Meta(ge=18)]


query = "SELECT name, age FROM people WHERE id = $1"
print(await conn.select_one(query, 1, into=Adult))  # Adult(name='person-1', age=19)
```

A Struct with plain fields is built directly, which is fastest.
A Struct that needs validation is a little slower.
To make sure a Struct always takes the fast path, pass `strategy="direct"`.
It then raises `UnsupportedTypeError` instead of validating:

```python
try:
    await conn.select_one(query, 1, into=Adult, strategy="direct")
except fastpg.UnsupportedTypeError as e:
    print(e)  # strategy='direct' unavailable: Adult.age: constraints {'ge': 18} need msgspec validation
```

Defaults and `__post_init__` work as usual.

## Dicts and NamedTuples

`into=dict` gives each row as a dict, keyed by column name:

```python
row = await conn.select_one("SELECT id, name, email FROM people WHERE id = $1", 7, into=dict)
print(row)  # {'id': 7, 'name': 'person-7', 'email': None}
```

Column names must be unique: a repeated name raises `TypeMismatchError`.

A `typing.NamedTuple` works like a Struct, with the same matching rules:

```python
from typing import NamedTuple


class NameAge(NamedTuple):
    name: str
    age: int
    verified: bool = False


query = "SELECT age, name FROM people WHERE id = $1"
print(await conn.select_one(query, 3, into=NameAge))  # NameAge(name='person-3', age=21, verified=False)
```

## Good to know

* A `str` field filled from a `json` or `jsonb` column gets the raw JSON text.
* Every decoding error is a `fastpg.DecodeError`. See [Errors](errors.md).
