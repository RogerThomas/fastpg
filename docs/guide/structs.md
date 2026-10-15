# Structs and decoding

Pass a `msgspec.Struct` subclass as `into=` and each row becomes an instance of it, built
from the binary values with no intermediate tuples or dicts.

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

## Strategies

| `strategy=` | how |
|---|---|
| `"auto"` (default) | `"direct"` when it gives the same result as validation would, else `"msgpack"` |
| `"direct"` | Rust decodes each value and calls the Struct's own `__init__` through vectorcall |
| `"msgpack"`, `"json"` | Rust encodes the rows as MessagePack maps or JSON, and a cached msgspec decoder builds and validates them |

Direct construction supports fields of type `bool`, `int`, `float`, `str`, `bytes`,
`datetime`, `date`, `time`, `uuid.UUID`, `decimal.Decimal` and `Any`, each optionally
`| None`, plus JSON-shaped fields (lists, dicts, nested Structs, ...) read from `json`/`jsonb`
columns, which a cached `msgspec.json.Decoder` validates.

A Struct that needs msgspec's validation goes through msgpack under `"auto"`, and raises
`UnsupportedTypeError` under `"direct"`. That's any Struct with a `msgspec.Meta` or `tz=`
constraint, an enum or `Literal` field, a union other than `T | None`, or another type only
validation produces:

```python
from typing import Annotated

import msgspec


class Adult(msgspec.Struct):
    name: str
    age: Annotated[int, msgspec.Meta(ge=18)]


query = "SELECT name, age FROM people WHERE id = $1"
print(await conn.select_one(query, 1, into=Adult))  # Adult(name='person-1', age=19)

try:
    await conn.select_one(query, 1, into=Adult, strategy="direct")
except fastpg.UnsupportedTypeError as e:
    print(e)
    # strategy='direct' unavailable:
    #   Adult.age: constraints {'ge': 18} need msgspec validation
```

`strategy="direct"` guarantees a Struct never takes the slower route. Defaults and
`__post_init__` work under every strategy.

## Binding columns to fields

Columns bind to fields once per (prepared statement, Struct type), before any row is
decoded, so mismatches raise even for an empty result:

* A column matches a field's encoded name, so `rename=` applies. Column order doesn't matter.
* A field with a default may be missing; a required one raises `MissingColumnError`.
* Extra columns are ignored, unless `forbid_unknown_fields=True` makes them raise
  `UnknownColumnError`.
* Under `"direct"`, a column's type must fit its field, else `TypeMismatchError`: `int` takes
  `int2`, `int4`, `int8` and `oid`; `float` also takes those; `str` takes the text types and
  enums; `datetime` takes `timestamp` (naive) and `timestamptz` (aware, UTC); the others take
  their own type (see [Types](queries.md#types)).

```python
import msgspec


class Contact(msgspec.Struct, rename={"display_name": "name"}):
    display_name: str
    verified: bool = False


query = "SELECT id, name FROM people WHERE id <= $1 ORDER BY id"
print(await conn.select_many(query, 2, into=Contact))
# [Contact(display_name='person-1', verified=False),
#  Contact(display_name='person-2', verified=False)]
```

## Gotchas

* A `str` field fed from a `json`/`jsonb` column gets the raw JSON text, under every strategy.
* `"msgpack"` and `"json"` encode rows as maps, so `array_like=True` Structs can only be
  built directly.
* A NULL in a non-optional field raises `NullValueError` only when its row is decoded. All
  decode errors are listed in [Errors](errors.md).
