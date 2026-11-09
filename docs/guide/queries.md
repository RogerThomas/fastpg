# Queries

A connection, a pool and a transaction all have the same query methods.

```python
row = await conn.select_one("SELECT id, name, email FROM people WHERE id = $1", 7)
print(row)  # (7, 'person-7', None)

rows = await conn.select_many("SELECT id, age FROM people WHERE id <= $1 ORDER BY id", 3)
print(rows)  # [(1, 19), (2, 20), (3, 21)]

await conn.execute_script("CREATE TEMP TABLE notes (id int, body text)")
count = await conn.execute("INSERT INTO notes VALUES ($1, $2)", 1, "first")
print(count)  # 1
```

| method | returns |
|---|---|
| `select_one(query, *args)` | one row; raises `NoRowsError` if there is none |
| `select_one_or_none(query, *args)` | one row, or `None` |
| `select_many(query, *args)` | a list of rows |
| `execute(query, *args)` | the number of rows changed |
| `execute_script(sql)` | nothing; runs one or more `;`-separated statements, with no parameters |

The `select_one` methods also raise `TooManyRowsError` if the query returns more than one row.

Rows are tuples by default.
Pass `into=` to get [Structs](structs.md), [pydantic models](pydantic.md) or
[dicts](structs.md#dicts-and-namedtuples) instead.

## Passing values

You can pass values three ways.
In all of them, values are sent to the server apart from the SQL, so they can't change
the query.

### `$1`, `$2`, ...

```python
row = await conn.select_one("SELECT name FROM people WHERE id = $1", 42)
print(row)  # ('person-42',)
```

### `:name` with `params=`

```python
import msgspec


class Filter(msgspec.Struct):
    id: int
    min_age: int


query = "SELECT name, age FROM people WHERE id = :id AND age >= :min_age"
print(await conn.select_one(query, params=Filter(id=42, min_age=30)))  # ('person-42', 60)
print(await conn.select_one(query, params={"id": 42, "min_age": 30}))  # ('person-42', 60)
```

`params=` takes a msgspec Struct, a dataclass, a pydantic model or a dict.
Fields the query doesn't use are ignored.
A name with no value raises `ParameterError`.
`::` casts still work.

### t-strings

Python 3.14's t-strings look like f-strings, but each `{value}` is sent as a parameter.
t-string queries need Python 3.14 or newer.

```python
person_id, min_age = 42, 30
row = await conn.select_one(
    t"SELECT name, age FROM people WHERE id = {person_id} AND age >= {min_age}"
)
print(row)  # ('person-42', 60)
```

For a table or column name, wrap it in `fastpg.Identifier`:

```python
table = fastpg.Identifier("public", "people")
column = fastpg.Identifier("name")
print(await conn.select_one(t"SELECT {column} FROM {table} WHERE id = {5}"))  # ('person-5',)
```

You can build a query from smaller t-strings:

```python
min_age, max_id = 70, 100
where = t"age >= {min_age} AND id <= {max_id}"
rows = await conn.select_many(t"SELECT id, age FROM people WHERE {where} ORDER BY id LIMIT {2}")
print(rows)  # [(52, 70), (53, 71)]
```

### SQL built at runtime

The query must be a string literal or a t-string.
A type checker such as pyright then flags an f-string query, where a value could change the SQL.

For a table or column name, use `Identifier` in a t-string, as above.
If you do need to build the SQL yourself, say so with `cast`:

```python
from typing import LiteralString, cast

columns = ", ".join(["id", "name"])
query = f"SELECT {columns} FROM people WHERE id = $1"
print(await conn.select_one(cast(LiteralString, query), 5))  # (5, 'person-5')
```

## Types

`NULL` is `None`, both ways.
See [Types](types.md) for which Python type each PostgreSQL type gives and takes.

## Prepared statements

fastpg prepares each query the first time it runs on a connection and reuses it after that.
You don't need to do anything.
Each connection keeps the last 256; change that with `statement_cache_size=` on `connect()`
or `create_pool()`.
The smallest is 1: prepared statements can't be turned off.
For pgbouncer, see [Limitations](../limitations.md).
