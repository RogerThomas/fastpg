"""Misuse: wrong argument types, odd queries, invalid pool configurations.

Each must fail with a clear error, at once, and leave the connection usable.
"""

import asyncio
import gc
import warnings
from typing import Any

import msgspec
import pytest

import fastpg
from fastpg import errors


class Row(msgspec.Struct):
    a: int


async def _assert_usable(conn: fastpg.Connection) -> None:
    assert await conn.select_one("SELECT 42") == (42,)


async def _pool_with(dsn: str, options: dict[str, Any]) -> None:
    pool = await fastpg.create_pool(dsn, **options)
    await pool.close()


@pytest.mark.parametrize(
    "into",
    [int, str, Row(1), [Row], None.__class__],
    ids=["int", "str", "a Struct instance", "a list", "NoneType"],
)
async def test_into_must_be_a_row_type(conn: fastpg.Connection, into: object) -> None:
    untyped: Any = into  # what an untyped caller could pass
    with pytest.raises(TypeError, match="into= must be dict, a NamedTuple, or a msgspec"):
        await conn.select_one("SELECT 1 AS a", into=untyped)
    await _assert_usable(conn)


@pytest.mark.parametrize("query", [42, b"SELECT 1", None])
async def test_query_must_be_a_str(conn: fastpg.Connection, query: object) -> None:
    untyped: Any = query
    with pytest.raises(TypeError, match="is not an instance of 'str'"):
        await conn.select_one(untyped)
    with pytest.raises(TypeError):
        await conn.execute(untyped)
    await _assert_usable(conn)


async def test_params_with_a_t_string_is_refused(conn: fastpg.Connection) -> None:
    value = 1
    with pytest.raises(TypeError, match="a t-string takes its values from its interpolations"):
        await conn.select_one(t"SELECT {value}::int", params={"value": 1})
    with pytest.raises(TypeError, match="a t-string takes its values from its interpolations"):
        await conn.execute(t"SELECT {value}::int", params={"value": 1})
    await _assert_usable(conn)


async def test_execute_script_takes_no_parameters(conn: fastpg.Connection) -> None:
    untyped: Any = conn.execute_script
    with pytest.raises(TypeError, match="takes 2 positional arguments but 3 were given"):
        untyped("SELECT $1", 1)
    value = 1
    with pytest.raises(TypeError, match="simple query protocol, which has no parameters"):
        await conn.execute_script(t"SELECT {value}")
    with pytest.raises(errors.DatabaseError, match="there is no parameter"):
        await conn.execute_script("SELECT $1")
    await _assert_usable(conn)


@pytest.mark.parametrize("query", ["", "   ", "-- only a comment", "/* only a comment */"])
async def test_empty_and_comment_only_queries(conn: fastpg.Connection, query: str) -> None:
    # The server answers EmptyQueryResponse: no rows, nothing affected.
    with pytest.raises(fastpg.NoRowsError):
        await conn.select_one(query)
    assert await conn.select_one_or_none(query) is None
    assert await conn.select_many(query) == []
    assert await conn.execute(query) == 0
    await conn.execute_script(query)
    await _assert_usable(conn)


async def test_multiple_statements_in_a_prepared_query(conn: fastpg.Connection) -> None:
    with pytest.raises(errors.SQLSyntaxError, match="cannot insert multiple commands"):
        await conn.select_one("SELECT 1; SELECT 2")
    with pytest.raises(errors.SQLSyntaxError, match="cannot insert multiple commands"):
        await conn.execute("SELECT 1; SELECT 2")
    await conn.execute_script("SELECT 1; SELECT 2")  # the simple protocol takes several
    await _assert_usable(conn)


async def test_wrong_parameter_counts_and_nul_bytes(conn: fastpg.Connection) -> None:
    with pytest.raises(errors.ParameterError, match="expects 1 parameter"):
        await conn.select_one("SELECT $1::int", 1, 2)
    with pytest.raises(errors.ParameterError, match="expects 1 parameter"):
        await conn.select_one("SELECT $1::int")
    with pytest.raises(fastpg.InterfaceError, match="embedded null"):
        await conn.select_one("SELECT 1\x00")
    await _assert_usable(conn)


@pytest.mark.parametrize("strategy", ["bogus", "Direct", ""])
async def test_an_unknown_strategy_is_refused(conn: fastpg.Connection, strategy: str) -> None:
    untyped: Any = strategy
    with pytest.raises(ValueError, match="invalid strategy"):
        await conn.select_one("SELECT 1 AS a", into=Row, strategy=untyped)
    with pytest.raises(ValueError, match="invalid strategy"):
        await conn.select_many("SELECT 1 AS a", into=dict, strategy=untyped)
    await _assert_usable(conn)


async def test_an_unknown_strategy_is_refused_by_the_pool(dsn: str) -> None:
    untyped: Any = "bogus"
    async with await fastpg.create_pool(dsn, min_size=1, max_size=1) as pool:
        with pytest.raises(ValueError, match="invalid strategy"):
            await pool.select_one("SELECT 1 AS a", into=Row, strategy=untyped)
        assert await pool.select_one("SELECT 1") == (1,)
        assert pool.stats().in_flight == 0


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"min_size": 3, "max_size": 2}, "min_size <= max_size"),
        ({"min_size": -1}, "min_size <= max_size"),
        ({"max_size": 0}, "max_size >= 1"),
        ({"max_inflight_per_conn": 0}, "max_inflight_per_conn must be >= 1"),
        ({"statement_cache_size": -1}, "statement_cache_size must be >= 0"),
        ({"max_lifetime": -1.0}, "max_lifetime must be a positive number of seconds"),
        ({"max_lifetime": 0.0}, "max_lifetime must be a positive number of seconds"),
        ({"max_lifetime": float("nan")}, "max_lifetime must be a positive number of seconds"),
        ({"max_lifetime": float("inf")}, "max_lifetime must be a positive number of seconds"),
        ({"max_idle": -5.0}, "max_idle must be a positive number of seconds"),
        ({"max_idle": float("inf")}, "max_idle must be a positive number of seconds"),
    ],
)
async def test_invalid_pool_configs_are_refused_before_connecting(
    dsn: str,
    options: dict[str, Any],
    message: str,
) -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(ValueError, match=message):
            await _pool_with(dsn, options)
        gc.collect()
    # No connection was opened (and leaked) before the configuration was rejected.
    assert [str(w.message) for w in caught if w.category is ResourceWarning] == []


async def test_valid_edge_pool_configs(dsn: str) -> None:
    async with await fastpg.create_pool(
        dsn,
        min_size=0,
        max_size=1,
        max_inflight_per_conn=1,
        statement_cache_size=0,
        max_lifetime=None,
        max_idle=None,
    ) as pool:
        assert pool.stats().size == 0
        assert await asyncio.gather(pool.select_one("SELECT 1"), pool.select_one("SELECT 2")) == [
            (1,),
            (2,),
        ]
