"""A query the server fails after sending some of its rows."""

import asyncio
from typing import NamedTuple

import msgspec
import pydantic
import pytest

import fastpg


class Value(msgspec.Struct):
    value: int


class ValueTuple(NamedTuple):
    value: int


class ValueModel(pydantic.BaseModel):
    value: int


type Target = tuple[type[object], fastpg.Strategy]


def _target_id(target: Target) -> str:
    into, strategy = target
    return f"{into.__name__}-{strategy}"


@pytest.fixture(
    name="target",
    params=[
        (dict, "auto"),
        (Value, "direct"),
        (Value, "msgpack"),
        (Value, "json"),
        (ValueTuple, "auto"),
        (ValueModel, "direct"),
        (ValueModel, "json"),
    ],
    ids=_target_id,
)
def _target(request: pytest.FixtureRequest) -> Target:
    return request.param


async def _write_then_fail_in_a_transaction(conn: fastpg.Connection) -> None:
    async with conn.transaction() as tx:
        await tx.execute("INSERT INTO written VALUES ($1)", 1)
        with pytest.raises(fastpg.DataError, match="division by zero"):
            await tx.select_many("SELECT 1/(i - 1000) FROM generate_series(1, 5000) AS s(i)")
        await tx.select_one("SELECT 1")  # the transaction is aborted: raises


# Rows before the failing one: within the first 512-row chunk, after it (decoded already
# while the rest streams), and after several.
@pytest.mark.parametrize("failing_row", [500, 1000, 4000])
async def test_error_after_some_rows_fails_select_many(
    conn: fastpg.Connection,
    target: Target,
    failing_row: int,
) -> None:
    into, strategy = target
    query = f"SELECT 1/(i - {failing_row}) AS value FROM generate_series(1, 5000) AS s(i)"  # noqa: S608
    with pytest.raises(fastpg.DataError, match="division by zero") as exc:
        await conn.select_many(query, into=into, strategy=strategy)
    assert exc.value.sqlstate == "22012"
    with pytest.raises(fastpg.DataError, match="division by zero"):
        await conn.select_many(query)
    # Nothing half-read is left on the connection.
    three = "SELECT i FROM generate_series(1, 3) AS s(i)"
    assert await conn.select_many(three) == [(1,), (2,), (3,)]


async def test_error_after_some_rows_fails_select_one_and_execute(conn: fastpg.Connection) -> None:
    query = "SELECT 1/(i - 2) FROM generate_series(1, 3) AS s(i)"
    with pytest.raises(fastpg.DataError, match="division by zero"):
        await conn.select_one(query)
    with pytest.raises(fastpg.DataError, match="division by zero"):
        await conn.select_one_or_none(query)
    with pytest.raises(fastpg.DataError, match="division by zero"):
        await conn.execute(query)
    assert await conn.select_one("SELECT 1") == (1,)


async def test_error_after_some_rows_aborts_the_transaction(conn: fastpg.Connection) -> None:
    await conn.execute_script("CREATE TEMP TABLE written (value int)")
    with pytest.raises(fastpg.DatabaseError, match="current transaction is aborted") as exc:
        await _write_then_fail_in_a_transaction(conn)
    assert exc.value.sqlstate == "25P02"
    assert await conn.select_many("SELECT value FROM written") == []  # rolled back
    assert await conn.select_one("SELECT 1") == (1,)


async def test_error_after_some_rows_leaves_queries_pipelined_behind_it(
    conn: fastpg.Connection,
) -> None:
    failing = conn.select_many("SELECT 1/(i - 1000) FROM generate_series(1, 5000) AS s(i)")
    behind = [conn.select_one("SELECT $1::int", i) for i in range(3)]
    results = await asyncio.gather(failing, *behind, return_exceptions=True)
    assert isinstance(results[0], fastpg.DataError)
    assert results[1:] == [(0,), (1,), (2,)]
