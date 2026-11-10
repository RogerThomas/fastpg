"""Misuse: wrong argument types, odd queries, invalid pool configurations.

Each must fail with a clear error, at once, and leave the connection usable.
"""

import asyncio
import gc
import warnings
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, LiteralString

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


async def test_a_pool_query_that_is_not_a_str_leaves_the_pool_usable(dsn: str) -> None:
    untyped: Any = 42
    async with await fastpg.create_pool(dsn, min_size=1, max_size=1) as pool:
        with pytest.raises(TypeError, match="is not an instance of 'str'"):
            await pool.select_one(untyped)
        with pytest.raises(TypeError, match="is not an instance of 'str'"):
            await pool.execute_script(untyped)
        async with asyncio.timeout(2):  # the connection went back to the idle stack
            assert await pool.select_one("SELECT 1") == (1,)
        assert pool.stats().idle == 1


async def test_a_waiting_pool_query_that_is_not_a_str_fails_alone(dsn: str) -> None:
    # Queued behind a busy connection, it's sent by whichever statement frees the slot: its
    # error must reach its own caller, not that statement's.
    untyped: Any = 42
    pool = await fastpg.create_pool(dsn, min_size=1, max_size=1, max_inflight_per_conn=1)
    async with pool:
        busy = asyncio.create_task(pool.select_one("SELECT 1 FROM pg_sleep(0.05)"))
        await asyncio.sleep(0.01)
        bad = asyncio.create_task(pool.select_one(untyped))
        after = asyncio.create_task(pool.select_one("SELECT 2"))
        async with asyncio.timeout(2):
            assert await busy == (1,)
            with pytest.raises(TypeError, match="is not an instance of 'str'"):
                await bad
            assert await after == (2,)
        stats = pool.stats()
        assert (stats.idle, stats.in_flight, stats.waiting) == (1, 0, 0)


async def test_execute_script_takes_no_parameters(conn: fastpg.Connection) -> None:
    untyped: Any = conn.execute_script
    with pytest.raises(TypeError, match="takes 2 positional arguments but 3 were given"):
        untyped("SELECT $1", 1)
    with pytest.raises(errors.DatabaseError, match="there is no parameter"):
        await conn.execute_script("SELECT $1")
    await _assert_usable(conn)


@pytest.mark.parametrize("query", ["", "   ", "-- only a comment", "/* only a comment */"])
async def test_empty_and_comment_only_queries(
    conn: fastpg.Connection,
    query: LiteralString,
) -> None:
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


_COPY_REFUSED = (
    r"not supported by select_\*\(\), execute\(\) or execute_script\(\): use copy_in\(\)"
)


@pytest.fixture
async def copy_table(dsn: str) -> AsyncIterator[None]:
    async with await fastpg.connect(dsn) as conn:
        await conn.execute_script(
            "DROP TABLE IF EXISTS misuse_copy; CREATE TABLE misuse_copy (a int)",
        )
        yield
        await conn.execute_script("DROP TABLE misuse_copy")


def _query_methods(
    conn: fastpg.Connection | fastpg.Transaction,
) -> list[Callable[[LiteralString], Awaitable[object]]]:
    return [
        conn.select_one,
        conn.select_one_or_none,
        conn.select_many,
        lambda query: conn.select_many(query, into=dict),
        conn.execute,
        conn.execute_script,
    ]


@pytest.mark.usefixtures("copy_table")
@pytest.mark.parametrize("query", ["COPY misuse_copy FROM STDIN", "COPY (SELECT 1) TO STDOUT"])
async def test_copy_from_or_to_the_client_is_refused(
    conn: fastpg.Connection,
    query: LiteralString,
) -> None:
    for method in _query_methods(conn):
        with pytest.raises(fastpg.InterfaceError, match=_COPY_REFUSED):
            async with asyncio.timeout(2):
                await method(query)
        async with asyncio.timeout(2):
            await _assert_usable(conn)
    with pytest.raises(fastpg.InterfaceError, match=_COPY_REFUSED):
        await conn.execute_script(
            "SELECT 1; /* a comment */ copy misuse_copy from stdin; SELECT 2",
        )
    async with asyncio.timeout(2):
        await _assert_usable(conn)


@pytest.mark.usefixtures("copy_table")
async def test_a_refused_copy_leaves_a_transaction_alone(
    conn: fastpg.Connection,
) -> None:
    async with conn.transaction() as tx:
        await tx.execute("INSERT INTO misuse_copy VALUES (1)")
        for method in _query_methods(tx):
            with pytest.raises(fastpg.InterfaceError, match=_COPY_REFUSED):
                async with asyncio.timeout(2):
                    await method("COPY misuse_copy FROM STDIN")
        async with asyncio.timeout(2):
            assert await tx.select_one("SELECT count(*) FROM misuse_copy") == (1,)
    assert await conn.select_one("SELECT count(*) FROM misuse_copy") == (1,)


@pytest.mark.usefixtures("copy_table")
async def test_a_refused_copy_leaves_the_pools_other_queries_alone(
    dsn: str,
) -> None:
    # One connection: the other callers' statements are pipelined on it with the COPY.
    async with await fastpg.create_pool(
        dsn,
        min_size=1,
        max_size=1,
        max_inflight_per_conn=8,
    ) as pool:
        calls = [
            pool.select_one("SELECT $1::int", 1),
            pool.execute_script("COPY misuse_copy FROM STDIN"),
            pool.select_one("SELECT $1::int", 2),
            pool.execute("COPY misuse_copy FROM STDIN"),
            pool.select_one("SELECT $1::int", 3),
            pool.select_many("COPY (SELECT 1) TO STDOUT"),
            pool.select_one("SELECT $1::int", 4),
        ]
        async with asyncio.timeout(2):
            results = await asyncio.gather(*calls, return_exceptions=True)
        assert results[0::2] == [(1,), (2,), (3,), (4,)]
        for refused in results[1::2]:
            assert isinstance(refused, fastpg.InterfaceError)
            assert "use copy_in()" in str(refused)
        async with asyncio.timeout(2):
            assert await pool.select_one("SELECT 5") == (5,)
        stats = pool.stats()
        assert (stats.size, stats.in_flight) == (1, 0)


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
        ({"statement_cache_size": 0}, "statement_cache_size must be >= 1, got 0"),
        ({"statement_cache_size": -1}, "statement_cache_size must be >= 1, got -1"),
        ({"max_lifetime": -1.0}, "max_lifetime must be a positive number of seconds"),
        ({"max_lifetime": 0.0}, "max_lifetime must be a positive number of seconds"),
        ({"max_lifetime": float("nan")}, "max_lifetime must be a positive number of seconds"),
        ({"max_lifetime": float("inf")}, "max_lifetime must be a positive number of seconds"),
        ({"max_idle": -5.0}, "max_idle must be a positive number of seconds"),
        ({"max_idle": float("inf")}, "max_idle must be a positive number of seconds"),
        ({"max_wait": 0.0}, "max_wait must be a positive number of seconds"),
        ({"max_wait": -1.0}, "max_wait must be a positive number of seconds"),
        ({"max_wait": float("nan")}, "max_wait must be a positive number of seconds"),
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


@pytest.mark.parametrize("size", [0, -1])
async def test_a_statement_cache_of_less_than_one_is_refused_by_connect(
    dsn: str,
    size: int,
) -> None:
    message = f"statement_cache_size must be >= 1, got {size}: fastpg always uses named"
    with pytest.raises(ValueError, match=message):
        await fastpg.connect(dsn, statement_cache_size=size)


async def test_valid_edge_pool_configs(dsn: str) -> None:
    async with await fastpg.create_pool(
        dsn,
        min_size=0,
        max_size=1,
        max_inflight_per_conn=1,
        statement_cache_size=1,
        max_lifetime=None,
        max_idle=None,
        max_wait=None,
    ) as pool:
        assert pool.stats().size == 0
        assert await asyncio.gather(pool.select_one("SELECT 1"), pool.select_one("SELECT 2")) == [
            (1,),
            (2,),
        ]


async def test_a_pool_is_not_constructed_directly() -> None:
    with pytest.raises(TypeError, match=r"use `await fastpg.create_pool\(dsn, ...\)`"):
        fastpg.Pool("postgresql://unused", min_size=0, max_size=1, max_inflight_per_conn=1)


async def test_a_transaction_is_not_constructed_directly(conn: fastpg.Connection) -> None:
    with pytest.raises(TypeError, match=r"use `conn.transaction\(\)` or `pool.transaction\(\)`"):
        fastpg.Transaction(conn)
    await _assert_usable(conn)
