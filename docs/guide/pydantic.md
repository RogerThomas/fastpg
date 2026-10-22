# pydantic models

If your app already uses pydantic, `into=` also takes a pydantic v2 model.
Install the extra first:

```sh
uv add 'fastpg[pydantic]'
```

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

Each column fills the field with the same alias, or the same name if it has no alias.
A field with a default can be left out of the query.

## Validation

fastpg only skips pydantic's validation when it would change nothing.
A model with plain field types and no validators or constraints is built directly, which is
fast.
Any other model is validated by pydantic as usual.

To make sure a model always takes the fast path, pass `strategy="direct"`.
It then raises `UnsupportedTypeError`, with the reason, instead of validating.

A row that fails validation raises `fastpg.DecodeError`.
pydantic's `ValidationError` is its `__cause__`.

msgspec [Structs](structs.md) are faster than pydantic models, so prefer them for new code.
