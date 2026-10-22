# Bulk writes

Three methods write many rows in one call.
Each returns the number of rows written.
Each is all or nothing: if one row fails, none are written.

| method | use it to |
|---|---|
| `copy_in(table, rows)` | load rows into a table, as fast as possible (binary `COPY`) |
| `insert_many(table, rows)` | load the same rows with `INSERT` statements |
| `execute_many(query, rows)` | run one statement once per row, e.g. an upsert |

```python
import msgspec


class Score(msgspec.Struct):
    player: str
    points: int


await conn.execute_script("CREATE TEMP TABLE scores (player text, points int)")
rows = [Score(f"p{i}", i * 10) for i in range(10_000)]
print(await conn.copy_in("scores", rows))  # 10000
print(await conn.select_one("SELECT count(*), sum(points) FROM scores"))  # (10000, 499950000)
```

`execute_many` takes one tuple of `$1, $2, ...` values per row:

```python
await conn.execute_script("CREATE TEMP TABLE totals (player text PRIMARY KEY, points int)")
upsert = """
    INSERT INTO totals VALUES ($1, $2)
    ON CONFLICT (player) DO UPDATE SET points = totals.points + excluded.points
"""
print(await conn.execute_many(upsert, [("a", 1), ("b", 2), ("a", 3)]))  # 3
print(await conn.select_many("SELECT * FROM totals ORDER BY player"))  # [('a', 4), ('b', 2)]
```

## Rows

`copy_in` and `insert_many` take rows of one kind:

| rows | columns written |
|---|---|
| msgspec Structs | the field names (after `rename=`) |
| pydantic models | the field aliases |
| dicts | the first row's keys |
| tuples | every column of the table, in order |

Pass `columns=` to choose the columns, e.g. `columns=["player"]`.
`table` can be `"name"` or `"schema.name"`.
Names are matched exactly, including case.

## Errors

```python
await conn.execute_script("CREATE TEMP TABLE items (id int PRIMARY KEY)")
async with conn.transaction() as tx:
    await tx.execute("INSERT INTO items VALUES ($1)", 0)
    try:
        await tx.copy_in("items", [(1,), (2,), (2,), (3,)])
    except fastpg.UniqueViolationError as e:
        print(e.row_index)  # 2
print(await conn.select_many("SELECT id FROM items"))  # [(0,)]
```

The error's `row_index` is the position of the failed row in your input.
`insert_many` writes up to 64 rows per statement, so it often can't tell which row failed.
Its `row_index` is then `None`.

Inside a transaction, a failed bulk write undoes only its own rows.
The transaction can carry on, as above.
