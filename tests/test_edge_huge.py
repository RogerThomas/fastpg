"""Huge values, very wide rows and very long results."""

import sysconfig
import tracemalloc

import msgspec
import pytest

import fastpg


class TwoTexts(msgspec.Struct):
    first: str
    second: str


class Row(msgspec.Struct):
    id: int
    label: str


@pytest.fixture(name="wide_struct", scope="module")
def _wide_struct() -> type[msgspec.Struct]:
    """A Struct of 1,600 int fields `c0` .. `c1599`: PostgreSQL's most columns in a table."""
    return msgspec.defstruct("Wide", [(f"c{i}", int) for i in range(1600)])


@pytest.mark.timeout(120)
async def test_100_mb_text_and_bytea(conn: fastpg.Connection) -> None:
    size = 100 * 1024 * 1024
    (text,) = await conn.select_one(f"SELECT repeat('a', {size})")
    assert len(text) == size
    assert text[:1] == text[-1:] == "a"
    del text
    (data,) = await conn.select_one(f"SELECT decode(repeat('ab', {size}), 'hex')")
    assert len(data) == size
    assert data[:1] == data[-1:] == b"\xab"
    del data
    assert await conn.select_one("SELECT length($1::text)", "a" * size) == (size,)
    assert await conn.select_one("SELECT length($1::bytea)", b"\xab" * size) == (size,)


@pytest.mark.timeout(120)
async def test_200_mb_row(conn: fastpg.Connection) -> None:
    size = 100 * 1024 * 1024
    query = f"SELECT repeat('a', {size}) AS first, repeat('b', {size}) AS second"
    row = await conn.select_one(query, into=TwoTexts)
    assert (len(row.first), len(row.second)) == (size, size)
    assert row.first[-1:] + row.second[:1] == "ab"


@pytest.mark.timeout(60)
async def test_1600_columns(conn: fastpg.Connection, wide_struct: type[msgspec.Struct]) -> None:
    columns = [f"c{i}" for i in range(1600)]
    await conn.execute_script(f"CREATE TEMP TABLE wide ({', '.join(f'{c} int' for c in columns)})")
    insert = f"INSERT INTO wide VALUES ({', '.join(f'${i + 1}' for i in range(1600))})"  # noqa: S608
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
    most = ", ".join(f"{i} AS c{i}" for i in range(1664))
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
