# Writes, bulk and COPY

Connections, transactions and pools have three atomic bulk-write methods, each returning the
number of rows written.

```python
import msgspec


class Score(msgspec.Struct):
    player: str
    points: int


await conn.execute_script("CREATE TEMP TABLE scores (player text, points int)")
rows = (Score(f"p{i}", i * 10) for i in range(10_000))
print(await conn.copy_in("scores", rows))  # 10000
print(await conn.select_one("SELECT count(*), sum(points) FROM scores"))  # (10000, 499950000)
```

| method | rows | how |
|---|---|---|
| `copy_in(table, rows, *, columns=None)` | Structs, pydantic models, mappings or sequences | binary `COPY ... FROM STDIN`, encoded in Rust, 4,096 rows per chunk; the fastest |
| `insert_many(table, rows, *, columns=None)` | the same | multi-row `INSERT ... VALUES`, 64 rows per statement (fewer if 65,535 parameters would be exceeded) |
| `execute_many(query, rows)` | sequences of parameters for `$1..$n` | one prepared statement per row, pipelined 1,024 rows per protocol `Sync`; any statement, `ON CONFLICT` and `UPDATE` included |

```python
await conn.execute_script("CREATE TEMP TABLE totals (player text PRIMARY KEY, points int)")
upsert = """INSERT INTO totals VALUES ($1, $2)
            ON CONFLICT (player) DO UPDATE SET points = totals.points + excluded.points"""
print(await conn.execute_many(upsert, [("a", 1), ("b", 2), ("a", 3)]))  # 3
print(await conn.select_many("SELECT * FROM totals ORDER BY player"))  # [('a', 4), ('b', 2)]
```

## Rows and columns

For `copy_in` and `insert_many`, every row must be of the first row's kind:

| rows | columns written |
|---|---|
| msgspec Structs | the fields' encoded names (`rename=` applies) |
| pydantic models | the fields' aliases |
| mappings | the first row's keys; every row must have exactly those keys |
| sequences | every column of the table, in order |

* `columns=` picks and orders the columns: a subset of the fields, the keys to take from
  mappings, or the columns a sequence fills. Use it with sequences for tables with generated
  columns.
* `table` is `"name"`, `"schema.name"` or a tuple of parts; each part is quoted, so names
  match case-sensitively.
* Values take the [parameter types](queries.md#types); `json`/`jsonb` columns also take
  anything msgspec serialises, but not pydantic models.

## Atomicity and errors

Each call runs in its own `BEGIN ... COMMIT`, or, inside your transaction, a `SAVEPOINT`: a
failed or cancelled bulk write leaves your transaction as it was, and usable.

```python
await conn.execute_script("CREATE TEMP TABLE items (id int PRIMARY KEY)")
async with conn.transaction() as tx:
    await tx.execute("INSERT INTO items VALUES ($1)", 0)
    try:
        await tx.copy_in("items", [(1,), (2,), (2,), (3,)])
    except fastpg.UniqueViolationError as e:
        print(e.row_index, e.constraint_name)  # 2 items_pkey
print(await conn.select_many("SELECT id FROM items"))  # [(0,)]
```

* Errors carry `row_index`, the failing row's 0-based position in your input, when it's
  known: always for encoding errors (`ParameterError`) and `execute_many`; from the server's
  `COPY` context for `copy_in`. For `insert_many` it's `None`, and a note names the 64-row
  range. Deferred constraints fail at `COMMIT`, with `row_index=None`.
* While a bulk write runs, other queries on its connection wait. The pool's bulk methods use a
  connection to themselves for the call.
