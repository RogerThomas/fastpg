# Pydantic models

`into=` also takes a pydantic v2 `BaseModel` subclass, with the `pydantic` extra installed
(`uv add /path/to/fastpg --extra pydantic`).

```python
import datetime

import pydantic


class Person(pydantic.BaseModel):
    account_id: int = pydantic.Field(alias="id")
    name: str
    created_at: datetime.datetime


query = "SELECT id, name, created_at FROM people WHERE id = $1"
person = await conn.select_one(query, 3, into=Person)
print(person.account_id, person.name)  # 3 person-3
```

| `strategy=` | how |
|---|---|
| `"auto"` (default) | `"direct"` when it's equivalent to validation, else `"json"` |
| `"direct"` | Rust builds each instance the way `model_construct` does, without validation |
| `"json"` | Rust encodes the rows as JSON for pydantic-core's `validate_json`: fully validated |

`"msgpack"` raises `UnsupportedTypeError` for pydantic models.

## When direct construction is used

Only when validation would change nothing, decided once per model from its core schema:

* every field is `bool`, `int`, `float`, `str`, `bytes`, `datetime`, `date`, `time`, `UUID`,
  `Decimal` or `Any` (each optionally `| None`), or JSON-shaped (list, dict, tuple, set,
  nested model, `TypedDict`, dataclass) from a `json`/`jsonb` column;
* no field constraints (`Field(gt=...)`, `max_length`, `strict`, ...);
* no validators, `model_post_init`, custom `__init__` or private attributes, and not a
  `RootModel`;
* `extra` isn't `"allow"`, and aliases are plain strings (no `AliasPath`, `AliasChoices`);
* defaults are `None`, `bool`, `int`, `float`, `str` or `bytes`, with no `default_factory`
  or `validate_default`.

Otherwise `"auto"` uses `"json"`, and `"direct"` raises `UnsupportedTypeError` with the reason.

## Binding and errors

* A column matches a field's `validation_alias`, else its `alias`, else its name. Fields with
  defaults may be missing; extra columns raise `UnknownColumnError` only with
  `extra="forbid"`.
* JSON-shaped fields are validated even under `"direct"`, by a cached `TypeAdapter`. A `str`
  field fed from `json`/`jsonb` gets the raw JSON text.
* Directly built instances get `model_fields_set` from the columns, as `model_construct`
  would.
* A `numeric` `NaN` or `±Infinity` for a `Decimal` field raises `fastpg.DecodeError` under
  both strategies, as pydantic's validation does; `Field(allow_inf_nan=True)` accepts them
  (through `"json"`).
* A validation failure raises `fastpg.DecodeError`, with pydantic's `ValidationError` as its
  `__cause__`.
