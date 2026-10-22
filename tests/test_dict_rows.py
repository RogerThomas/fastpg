"""`into=dict`: one dict per row, keyed by column name."""

import datetime as dt
import gc
import uuid
from decimal import Decimal
from typing import Any, assert_type

import pytest

import fastpg


async def test_every_column_type(conn: fastpg.Connection) -> None:
    query = """
SELECT true AS b, 1::int2 AS i2, 2::int4 AS i4, 3::int8 AS i8, 1.5::float4 AS f4,
       2.5::float8 AS f8, 'text' AS s, 'varchar'::varchar AS vc, 'ab'::char(3) AS c,
       'a'::"char" AS ch, '\\x0102'::bytea AS raw,
       'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11'::uuid AS u, '2024-02-29'::date AS d,
       '12:34:56.789'::time AS t, '2024-02-29 12:34:56.789'::timestamp AS ts,
       '2024-02-29 12:34:56.789+00'::timestamptz AS tstz, 12.340::numeric AS n,
       '{"k": [1]}'::jsonb AS jb, '[1, "two"]'::json AS j
"""
    row = await conn.select_one(query, into=dict)
    assert row == {
        "b": True,
        "i2": 1,
        "i4": 2,
        "i8": 3,
        "f4": 1.5,
        "f8": 2.5,
        "s": "text",
        "vc": "varchar",
        "c": "ab ",
        "ch": "a",
        "raw": b"\x01\x02",
        "u": uuid.UUID("a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11"),
        "d": dt.date(2024, 2, 29),
        "t": dt.time(12, 34, 56, 789_000),
        "ts": dt.datetime(2024, 2, 29, 12, 34, 56, 789_000),  # noqa: DTZ001 - naive on purpose
        "tstz": dt.datetime(2024, 2, 29, 12, 34, 56, 789_000, tzinfo=dt.UTC),
        "n": Decimal("12.340"),
        "jb": {"k": [1]},
        "j": [1, "two"],
    }
    assert tuple(row.values()) == await conn.select_one(query)


async def test_keys_follow_column_order(conn: fastpg.Connection) -> None:
    row = await conn.select_one("SELECT 1 AS b, 2 AS a, 3 AS c", into=dict)
    assert list(row.items()) == [("b", 1), ("a", 2), ("c", 3)]


async def test_nulls(conn: fastpg.Connection) -> None:
    types = ["bool", "bytea", "date", "float4", "float8", "int2", "int4", "int8", "json", "jsonb"]
    types += ["numeric", "text", "time", "timestamp", "timestamptz", "uuid", "varchar"]
    columns = ", ".join(f"NULL::{t} AS {t}" for t in types)
    row = await conn.select_one(f"SELECT {columns}", into=dict)
    assert row == dict.fromkeys(types)


async def test_duplicate_column_names_raise(conn: fastpg.Connection) -> None:
    with pytest.raises(fastpg.TypeMismatchError, match=r'column "a" appears more than once'):
        await conn.select_one("SELECT 1 AS a, 2 AS a", into=dict)
    # Unnamed expressions are all called "?column?".
    with pytest.raises(fastpg.TypeMismatchError, match=r'"\?column\?" .*rename one with AS'):
        await conn.select_many("SELECT 1, 2 WHERE false", into=dict)


async def test_unsupported_column_type(conn: fastpg.Connection) -> None:
    with pytest.raises(fastpg.UnsupportedTypeError, match=r"interval\[\]"):
        await conn.select_one("SELECT ARRAY['1 day'::interval] AS a", into=dict)


async def test_select_one_or_none_and_empty_results(conn: fastpg.Connection) -> None:
    query = "SELECT id, name FROM people WHERE id = $1"
    assert await conn.select_one_or_none(query, -1, into=dict) is None
    assert await conn.select_one_or_none(query, 3, into=dict) == {"id": 3, "name": "person-3"}
    assert await conn.select_many(query, -1, into=dict) == []
    with pytest.raises(fastpg.NoRowsError):
        await conn.select_one(query, -1, into=dict)


async def test_large_result_streams_in_chunks(conn: fastpg.Connection) -> None:
    # Spans several 512-row chunks with a partial final chunk.
    query = "SELECT id, name, email, balance FROM people WHERE id <= 5000 ORDER BY id"
    rows = await conn.select_many(query, into=dict)
    tuples = await conn.select_many(query)
    assert len(rows) == 5000
    assert rows == [dict(zip(("id", "name", "email", "balance"), t, strict=True)) for t in tuples]


@pytest.mark.parametrize("strategy", ["msgpack", "json"])
async def test_other_strategies_are_unsupported(
    conn: fastpg.Connection,
    strategy: fastpg.Strategy,
) -> None:
    with pytest.raises(fastpg.UnsupportedTypeError, match="dict rows are only built directly"):
        await conn.select_one("SELECT 1 AS a", into=dict, strategy=strategy)


async def test_direct_strategy(conn: fastpg.Connection) -> None:
    assert await conn.select_one("SELECT 1 AS a", into=dict, strategy="direct") == {"a": 1}


async def test_rows_stay_gc_tracked(conn: fastpg.Connection) -> None:
    # A dict can be given a container later, and CPython 3.14 wouldn't re-track an untracked
    # one. (3.13 untracks a dict of atoms itself, in a collection, and re-tracks it then.)
    rows = await conn.select_many("SELECT id FROM people WHERE id <= 3 ORDER BY id", into=dict)
    gc.collect()
    for row in rows:
        row["children"] = []
    assert [gc.is_tracked(row) for row in rows] == [True, True, True]


async def test_return_types(conn: fastpg.Connection) -> None:
    one = await conn.select_one("SELECT 1 AS a", into=dict)
    assert_type(one, dict[str, Any])
    maybe = await conn.select_one_or_none("SELECT 1 AS a", into=dict)
    assert_type(maybe, dict[str, Any] | None)
    many = await conn.select_many("SELECT 1 AS a", into=dict)
    assert_type(many, list[dict[str, Any]])
    assert (one, maybe, many) == ({"a": 1}, {"a": 1}, [{"a": 1}])


async def test_in_a_transaction(conn: fastpg.Connection) -> None:
    async with conn.transaction() as tx:
        one = await tx.select_one("SELECT 1 AS a", into=dict)
        maybe = await tx.select_one_or_none("SELECT 1 AS a WHERE false", into=dict)
        many = await tx.select_many("SELECT generate_series(1, 2) AS a", into=dict)
    assert_type(one, dict[str, Any])
    assert_type(maybe, dict[str, Any] | None)
    assert_type(many, list[dict[str, Any]])
    assert (one, maybe, many) == ({"a": 1}, None, [{"a": 1}, {"a": 2}])


async def test_through_a_pool(dsn: str) -> None:
    async with await fastpg.create_pool(dsn) as pool:
        one = await pool.select_one("SELECT 1 AS a", into=dict)
        maybe = await pool.select_one_or_none("SELECT 1 AS a WHERE false", into=dict)
        many = await pool.select_many("SELECT generate_series(1, 2) AS a", into=dict)
        assert_type(one, dict[str, Any])
        assert_type(maybe, dict[str, Any] | None)
        assert_type(many, list[dict[str, Any]])
        assert (one, maybe, many) == ({"a": 1}, None, [{"a": 1}, {"a": 2}])
        async with pool.transaction() as tx:
            assert await tx.select_one("SELECT 1 AS a", into=dict) == {"a": 1}
