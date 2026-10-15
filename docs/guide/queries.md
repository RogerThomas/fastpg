# Queries and results

A `Connection`, a `Transaction` and a `Pool` all run queries with the same methods.

```python
row = await conn.select_one("SELECT id, name, email FROM people WHERE id = $1", 7)
print(row)  # (7, 'person-7', None)

rows = await conn.select_many("SELECT id, age FROM people WHERE id <= $1 ORDER BY id", 3)
print(rows)  # [(1, 19), (2, 20), (3, 21)]

await conn.execute_script("CREATE TEMP TABLE notes (id int, body text)")
print(await conn.execute("INSERT INTO notes VALUES ($1, $2)", 1, "first"))  # 1
```

| method | returns |
|---|---|
| `select_one(query, *args, into=T)` | `T`; `NoRowsError` if no row, `TooManyRowsError` if more than one |
| `select_one_or_none(query, *args, into=T)` | `T \| None`; `TooManyRowsError` if more than one row |
| `select_many(query, *args, into=T)` | `list[T]` |
| `execute(query, *args)` | `int`: the rows affected, from the command tag (0 for DDL) |
| `execute_script(query)` | `None`: `;`-separated statements, no parameters, one implicit transaction |

* Without `into=`, rows are tuples; with it, [Structs](structs.md),
  [pydantic models](pydantic.md), or [dicts and NamedTuples](structs.md#dicts-and-namedtuples),
  typed accordingly.

## Passing values

### Positional parameters

`$1, $2, ...` with `*args`:

```python
print(await conn.select_one("SELECT name FROM people WHERE id = $1", 42))  # ('person-42',)
```

### Named parameters

`:name` with `params=`:

```python
import msgspec


class PersonSelect(msgspec.Struct):
    id: int
    min_age: int


query = "SELECT name, age FROM people WHERE id = :id AND age >= :min_age"
print(await conn.select_one(query, params=PersonSelect(id=42, min_age=30)))
print(await conn.select_one(query, params={"id": 42, "min_age": 30}))
# ('person-42', 60), twice; runs: ... WHERE id = $1 AND age >= $2
```

* `params=` is a msgspec Struct, dataclass, pydantic model or `Mapping[str, object]`.
  Unused fields and keys are ignored.
* A name used twice is one parameter. `:name` in quotes, comments and dollar quotes is
  text; `::` is a cast.
* `\:` is a literal colon, e.g. for array slices: `r"arr[lo\:hi]"` (a raw string).
* Without `params=` the query isn't scanned, so `:name` is sent as written.

Errors, raised before anything is sent:

* a placeholder with no value in `params=`: `ParameterError`;
* `params=` with positional parameters or a t-string, or of another type: `TypeError`;
* a `$1` placeholder outside quotes in a query with `params=`: `ValueError`.

### Template strings

A Python 3.14 t-string ([PEP 750](https://peps.python.org/pep-0750/)). Values become bound
parameters, never SQL text:

```python
person_id, min_age = 42, 30
row = await conn.select_one(
    t"SELECT name, age FROM people WHERE id = {person_id} AND age >= {min_age}"
)
print(row)  # ('person-42', 60)
```

Unlike `params=`, t-strings can also take dynamic table and column names, as
`fastpg.Identifier`:

```python
table = fastpg.Identifier("public", "people")
column = fastpg.Identifier("name")
print(await conn.select_one(t"SELECT {column} FROM {table} WHERE id = {5}"))  # ('person-5',)
```

## Types

Parameters are encoded for the server-declared type; a misfit raises `ParameterError`
before sending. Results are binary, decoded in Rust. NULL is `None` both ways; an
`enum.Enum` is sent as its `.value`.

| PostgreSQL | result | parameters accepted |
|---|---|---|
| `bool` | `bool` | `bool` |
| `int2`, `int4`, `int8`, `oid` | `int` | `int`, range-checked |
| `float4`, `float8` | `float` | `float`, `int` |
| `numeric` | `Decimal` | `Decimal`, `int`, `float`, `str` |
| `text`, `varchar`, `bpchar`, `name`, `citext`, enums | `str` | `str` |
| `"char"` | `str`, one character or `""` | `str`, at most one character |
| `bytea` | `bytes` | `bytes`, `bytearray`, `memoryview` |
| `uuid` | `uuid.UUID` | `uuid.UUID`, `str` |
| `date`, `time` | `date`, `time` | `date`, `time` |
| `timestamp` | naive `datetime` | naive `datetime` only |
| `timestamptz` | aware `datetime`, in UTC | aware `datetime` only |
| `json`, `jsonb` | decoded with msgspec | `dict`, `list` (encoded with msgspec), `str` (JSON text) |
| `void` | `None` | |

## Prepared statements

Statements are prepared on first use and kept in a per-connection LRU cache keyed by SQL
text. `statement_cache_size=256` on `connect` or `create_pool` sets its size; it can't be
turned off.

## Gotchas

* No array parameters: a `list` is sent as JSON, so `= ANY($1)` fails. Use
  `WHERE id IN (SELECT jsonb_array_elements_text($1)::int)` instead.
* Arrays, `interval`, `timetz`, ranges and composite types raise `UnsupportedTypeError`, and
  `infinity` dates and timestamps raise `DecodeError`. Cast in SQL, e.g. `tags::text`.
* Concurrent tasks' queries on one connection are pipelined and unordered relative to each
  other, and a slow one delays those behind it: see
  [Connection pool](pool.md#head-of-line-blocking).
* Statements are always named, so pgbouncer in transaction-pooling mode isn't supported.
