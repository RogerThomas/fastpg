# Types

This page shows which Python type you get for each PostgreSQL type, and which Python types
you can pass as query parameters. Bulk writes take the same Python types.

`NULL` is `None`, both ways.
A Python `enum.Enum` is sent as its `.value`.

Tuple and dict rows hold the types in the "you get" column.
A field of a Struct, NamedTuple or pydantic model must have that type too, with two extras:
an integer column can fill a `float` field, and a `json` or `jsonb` column can fill a `str`
field (with the raw JSON text) or a typed field such as `list[int]` or a nested Struct.

| PostgreSQL | you get | you can pass | note |
|---|---|---|---|
| **Numbers** | | | |
| `bool` | `bool` | `bool` | |
| `smallint`, `integer`, `bigint` | `int` | `int` | |
| `oid` | `int` | `int` | |
| `real`, `double precision` | `float` | `float`, `int` | |
| `numeric` | `Decimal` | `Decimal`, `int`, `float`, `str` | |
| **Text** | | | |
| `text`, `varchar`, `name`, `citext` | `str` | `str` | |
| `char(n)` | `str` | `str` | padded with spaces to `n` |
| `"char"` | `str` | `str` | one character, or `""` |
| **Bytes** | | | |
| `bytea` | `bytes` | `bytes`, `bytearray`, `memoryview` | |
| **Dates and times** | | | |
| `date` | `date` | `date` | |
| `time` | `time` | `time` without a time zone | |
| `timestamp` | `datetime` without a time zone | the same | |
| `timestamptz` | `datetime` in UTC | `datetime` with a time zone | |
| **UUID** | | | |
| `uuid` | `uuid.UUID` | `uuid.UUID`, `str` | |
| **JSON** | | | |
| `json`, `jsonb` | `dict`, `list`, `str`, `int`, `float`, `bool` or `None` | a `str` of JSON text, or any value msgspec can encode | a `str` is sent as it is |
| **Arrays** | | | |
| arrays of the types above (`int[]`, `text[]`, `jsonb[]`, ...) | `list` | `list`, `tuple` | one dimension only |
| **Enums** | | | |
| enum types | `str` | `str`, `enum.Enum` | |
| **Other** | | | |
| domains | as their base type | as their base type | |
| `void`, e.g. `SELECT pg_sleep(1)` | `None` | | |

Good to know:

* `timestamp` gives and takes `datetime`s without a time zone; `timestamptz` gives them in UTC
  and takes only ones with a time zone. The wrong kind raises `ParameterError`.
* `numeric` gives a `Decimal` with every digit kept: `1.10` comes back as `Decimal('1.10')`.
  `NaN` and `Infinity` come back as `Decimal` too.
* In a `json` or `jsonb` value, `bytes` are stored as base64 text and read back as a `str`.
* NaN and infinities are refused in JSON values: they raise `ParameterError`.
* A value that doesn't fit its column, such as `70000` for a `smallint`, raises
  `ParameterError` before anything is sent.

Lists and arrays: see [Arrays and grouping](lists.md).

## Types fastpg doesn't support

Reading a column of any other type, such as `interval`, `timetz`, ranges, composite types,
`money` or `inet`, raises `UnsupportedTypeError`, even when no rows come back.
So do arrays of those types.

Sending a Python value of any other type, such as `timedelta` or `set`, raises
`ParameterError`. So does any value for a parameter of a type fastpg doesn't support.

For both, cast to and from `text` in SQL:

```python
query = "SELECT ($1::text::interval * 2)::text"
print(await conn.select_one(query, "36 hours"))  # ('72:00:00',)
```

Some values of supported types can't be read either. They raise `DecodeError`:

* `infinity` and `-infinity` dates and timestamps, and dates outside the years 1 to 9999;
* arrays with more than one dimension, or that don't start at index 1 (the default).
