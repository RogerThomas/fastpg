"""Huge values, very wide rows and very long results."""

import sysconfig
import tracemalloc
from typing import Any, LiteralString, NamedTuple, cast

import msgspec
import pytest
from pydantic import BaseModel

import fastpg


class TwoTexts(msgspec.Struct):
    first: str
    second: str


class Row(msgspec.Struct):
    id: int
    label: str


class Sized(msgspec.Struct):
    id: int
    v: str | None


class SizedTuple(NamedTuple):
    id: int
    v: str | None


class SizedModel(BaseModel):
    id: int
    v: str | None


class SizedGroup(msgspec.Struct):
    id: int
    v: list[str | None]


# A streamed fetch ends a chunk at 512 rows or once its rows take 1 MiB, counting each
# value's bytes and 8 bytes per column (here an int4 and a text: 4 + len(v) + 16).
MIB = 1024 * 1024
SIZES: list[int | None] = [
    1,
    None,
    0,
    5,
    MIB - 20 - 3 * 25 - 24 - 5 - 1,  # one byte short of the limit, with the rows before it
    3,  # crosses it
    MIB - 20,
    MIB - 21,
    MIB - 19,  # each exactly at, just under, just over the limit alone
    None,
    3 * MIB,  # one row larger than the limit
    *([7] * 700),  # tiny rows across the 512-row limit
    None,
    MIB + 1,
    2,
    None,
]
SIZED_QUERY = """
SELECT i::int AS id, repeat(chr(97 + (i % 26)::int), s) AS v
FROM unnest($1::int[]) WITH ORDINALITY AS t(s, i) ORDER BY i
"""


def _sized_rows() -> list[tuple[int, str | None]]:
    return [(i, None if s is None else chr(97 + i % 26) * s) for i, s in enumerate(SIZES, start=1)]


@pytest.fixture(name="wide_struct", scope="module")
def _wide_struct() -> type[msgspec.Struct]:
    """A Struct of 1,600 int fields `c0` .. `c1599`: PostgreSQL's most columns in a table."""
    return msgspec.defstruct("Wide", [(f"c{i}", int) for i in range(1600)])


@pytest.mark.timeout(120)
async def test_100_mb_text_and_bytea(conn: fastpg.Connection) -> None:
    size = 100 * 1024 * 1024
    (text,) = await conn.select_one(cast("LiteralString", f"SELECT repeat('a', {size})"))
    assert len(text) == size
    assert text[:1] == text[-1:] == "a"
    del text
    (data,) = await conn.select_one(
        cast("LiteralString", f"SELECT decode(repeat('ab', {size}), 'hex')"),
    )
    assert len(data) == size
    assert data[:1] == data[-1:] == b"\xab"
    del data
    assert await conn.select_one("SELECT length($1::text)", "a" * size) == (size,)
    assert await conn.select_one("SELECT length($1::bytea)", b"\xab" * size) == (size,)


@pytest.mark.timeout(120)
async def test_200_mb_row(conn: fastpg.Connection) -> None:
    size = 100 * 1024 * 1024
    query = cast(
        "LiteralString",
        f"SELECT repeat('a', {size}) AS first, repeat('b', {size}) AS second",
    )
    row = await conn.select_one(query, into=TwoTexts)
    assert (len(row.first), len(row.second)) == (size, size)
    assert row.first[-1:] + row.second[:1] == "ab"


@pytest.mark.timeout(60)
async def test_1600_columns(conn: fastpg.Connection, wide_struct: type[msgspec.Struct]) -> None:
    columns = cast("list[LiteralString]", [f"c{i}" for i in range(1600)])
    await conn.execute_script(f"CREATE TEMP TABLE wide ({', '.join(f'{c} int' for c in columns)})")
    placeholders = cast("LiteralString", ", ".join(f"${i + 1}" for i in range(1600)))
    insert = f"INSERT INTO wide VALUES ({placeholders})"  # noqa: S608
    assert await conn.execute(insert, *range(1600)) == 1
    assert await conn.select_one("SELECT * FROM wide") == tuple(range(1600))
    as_dict = dict(zip(columns, range(1600), strict=True))
    assert await conn.select_one("SELECT * FROM wide", into=dict) == as_dict
    for strategy in ("direct", "msgpack", "json"):
        row = await conn.select_one("SELECT * FROM wide", into=wide_struct, strategy=strategy)
        assert msgspec.structs.astuple(row) == tuple(range(1600))
    # Columns in another order than the fields: passed by keyword instead of position.
    reversed_ = f"SELECT {', '.join(reversed(columns))} FROM wide"  # noqa: S608
    row = await conn.select_one(reversed_, into=wide_struct)
    assert msgspec.structs.astuple(row) == tuple(range(1600))


@pytest.mark.timeout(60)
async def test_most_columns_a_select_list_takes(
    conn: fastpg.Connection,
    wide_struct: type[msgspec.Struct],
) -> None:
    most = cast("LiteralString", ", ".join(f"{i} AS c{i}" for i in range(1664)))
    rows = await conn.select_many(f"SELECT {most} FROM generate_series(1, 1000)", into=wide_struct)  # noqa: S608
    assert len(rows) == 1000
    assert msgspec.structs.astuple(rows[-1]) == tuple(range(1600))  # extra columns ignored
    with pytest.raises(fastpg.DatabaseError, match="target lists can have at most 1664 entries"):
        await conn.select_one(f"SELECT {most}, 1664 AS c1664")
    assert await conn.select_one("SELECT 1") == (1,)


@pytest.mark.timeout(120)
async def test_million_rows_peak_memory_stays_near_the_result_size(
    conn: fastpg.Connection,
) -> None:
    query = "SELECT i AS id, 'row-' || i AS label FROM generate_series(1, 1000000) AS s(i)"
    tracemalloc.start()
    try:
        rows = await conn.select_many(query, into=Row)
        result, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(rows) == 1_000_000
    assert rows[-1] == Row(1_000_000, "row-1000000")
    # Rows are decoded in 512-row chunks while the rest arrives, so Python's peak is the
    # result itself; the raw row buffers (Rust, not traced) are one chunk at a time. A
    # free-threaded build frees a growing list's old storage only once every thread has moved
    # on, so the result list's earlier buffers can still count at the peak there.
    margin = 1.25 if sysconfig.get_config_var("Py_GIL_DISABLED") else 1.1
    assert peak < result * margin


@pytest.mark.timeout(120)
@pytest.mark.parametrize(
    ("into", "strategy"),
    [
        (None, "auto"),
        (dict, "auto"),
        (SizedTuple, "auto"),
        (Sized, "direct"),
        (Sized, "msgpack"),
        (Sized, "json"),
        (SizedModel, "direct"),
        (SizedModel, "json"),
    ],
)
async def test_rows_across_chunk_limits_arrive_whole_and_in_order(
    conn: fastpg.Connection,
    into: type[Any] | None,
    strategy: fastpg.Strategy,
) -> None:
    pairs = _sized_rows()
    if into is None:
        rows = await conn.select_many(SIZED_QUERY, SIZES)
        expected: list[Any] = pairs
    else:
        rows = await conn.select_many(SIZED_QUERY, SIZES, into=into, strategy=strategy)
        expected = [into(id=i, v=v) for i, v in pairs]
    assert rows == expected


@pytest.mark.timeout(120)
async def test_rows_across_chunk_limits_through_a_pool_and_grouping(dsn: str) -> None:
    expected = _sized_rows()
    async with await fastpg.create_pool(dsn) as pool:
        assert await pool.select_many(SIZED_QUERY, SIZES) == expected
        query = f"SELECT 1 AS id, v FROM ({SIZED_QUERY}) AS r"  # noqa: S608
        groups = await pool.select_and_group(query, SIZES, into=SizedGroup)
    assert groups == [SizedGroup(1, [v for _, v in expected])]
