"""A connection reset (RST) or closed (FIN) in the middle of a result, a COPY or a bulk write.

What must hold: the call fails at once with ``ConnectionClosedError`` saying the connection
was lost, the connection is closed, nothing of an interrupted write is committed, and a
pool replaces the connection.
"""

# ruff: noqa: S608 - SQL is built from fixture-generated table names

import asyncio
from typing import TYPE_CHECKING

import pytest

import fastpg

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from faults import FaultProxy

_LOST = "connection lost"
_BIG = "SELECT n, repeat('x', 100) FROM generate_series(1, 20000) AS n"  # ~2.3 MB


@pytest.fixture
async def drop_items(conn: fastpg.Connection) -> AsyncIterator[str]:
    await conn.execute_script(
        "DROP TABLE IF EXISTS net_drop_items; CREATE TABLE net_drop_items (id int, v text)",
    )
    yield "net_drop_items"
    await conn.execute_script("DROP TABLE net_drop_items")


async def _settle(pool: fastpg.Pool) -> None:
    """Wait until nothing is in flight, waiting or opening."""
    async with asyncio.timeout(5):
        while (s := pool.stats()).in_flight or s.waiting or s.waiting_acquire or s.opening:
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("rst", [True, False], ids=["rst", "fin"])
async def test_cut_in_the_middle_of_a_streamed_result(
    proxied_dsn: str,
    fault_proxy: FaultProxy,
    *,
    rst: bool,
) -> None:
    async with await fastpg.connect(proxied_dsn) as conn:
        assert len(await conn.select_many(_BIG)) == 20_000  # prepared, and the size is right
        fault_proxy.to_client.cut_after(1_000_000, rst=rst)  # mid-way through the next one
        async with asyncio.timeout(5):
            with pytest.raises(fastpg.ConnectionClosedError, match=_LOST):
                await conn.select_many(_BIG)
        assert conn.closed
        with pytest.raises(fastpg.ConnectionClosedError, match=_LOST):
            await conn.select_one("SELECT 1")


@pytest.mark.parametrize("rst", [True, False], ids=["rst", "fin"])
async def test_cut_fails_every_pipelined_query(
    proxied_dsn: str,
    fault_proxy: FaultProxy,
    *,
    rst: bool,
) -> None:
    async with await fastpg.connect(proxied_dsn) as conn:
        queries = [conn.select_one("SELECT pg_sleep(0.5), $1::int", i) for i in range(5)]
        pending = [asyncio.ensure_future(query) for query in queries]
        await asyncio.sleep(0.1)
        fault_proxy.drop(rst=rst)
        async with asyncio.timeout(5):
            results = await asyncio.gather(*pending, return_exceptions=True)
        assert all(isinstance(r, fastpg.ConnectionClosedError) for r in results), results


async def test_close_after_close_says_closed_not_lost(proxied_dsn: str) -> None:
    conn = await fastpg.connect(proxied_dsn)
    await conn.close()
    with pytest.raises(fastpg.ConnectionClosedError, match="connection is closed"):
        await conn.select_one("SELECT 1")


@pytest.mark.parametrize("rst", [True, False], ids=["rst", "fin"])
async def test_cut_in_the_middle_of_copy_in(
    proxied_dsn: str,
    fault_proxy: FaultProxy,
    conn: fastpg.Connection,
    drop_items: str,
    *,
    rst: bool,
) -> None:
    async with await fastpg.connect(proxied_dsn) as victim:
        fault_proxy.to_server.cut_after(500_000, rst=rst)  # of ~5.5 MB
        rows = ((i, "x" * 100) for i in range(50_000))
        async with asyncio.timeout(5):
            with pytest.raises(fastpg.ConnectionClosedError, match=_LOST):
                await victim.copy_in(drop_items, rows)
        assert victim.closed
    assert await conn.select_one(f"SELECT count(*) FROM {drop_items}") == (0,)


@pytest.mark.parametrize("rst", [True, False], ids=["rst", "fin"])
async def test_cut_in_the_middle_of_execute_many(
    proxied_dsn: str,
    fault_proxy: FaultProxy,
    conn: fastpg.Connection,
    drop_items: str,
    *,
    rst: bool,
) -> None:
    async with await fastpg.connect(proxied_dsn) as victim:
        fault_proxy.to_server.cut_after(100_000, rst=rst)
        query = f"INSERT INTO {drop_items} VALUES ($1, $2)"
        async with asyncio.timeout(5):
            with pytest.raises(fastpg.ConnectionClosedError, match=_LOST):
                await victim.execute_many(query, [(i, "x" * 20) for i in range(20_000)])
    assert await conn.select_one(f"SELECT count(*) FROM {drop_items}") == (0,)


async def test_pool_replaces_a_connection_cut_mid_result(
    proxied_dsn: str,
    fault_proxy: FaultProxy,
) -> None:
    async with await fastpg.create_pool(proxied_dsn, min_size=1, max_size=1) as pool:
        assert len(await pool.select_many(_BIG)) == 20_000
        fault_proxy.to_client.cut_after(1_000_000, rst=True)
        async with asyncio.timeout(5):
            with pytest.raises(fastpg.ConnectionClosedError, match=_LOST):
                await pool.select_many(_BIG)
            assert await pool.select_one("SELECT 1") == (1,)  # on a new connection
        await _settle(pool)
        stats = pool.stats()
        assert (stats.size, stats.idle, stats.in_flight) == (1, 1, 0)
        assert fault_proxy.accepted == 2


async def test_pool_replaces_a_connection_cut_mid_copy(
    proxied_dsn: str,
    fault_proxy: FaultProxy,
    conn: fastpg.Connection,
    drop_items: str,
) -> None:
    async with await fastpg.create_pool(proxied_dsn, min_size=1, max_size=1) as pool:
        fault_proxy.to_server.cut_after(500_000, rst=True)
        rows = ((i, "x" * 100) for i in range(50_000))
        async with asyncio.timeout(5):
            with pytest.raises(fastpg.ConnectionClosedError, match=_LOST):
                await pool.copy_in(drop_items, rows)
            assert await pool.copy_in(drop_items, [(1, "x")]) == 1
        await _settle(pool)
        stats = pool.stats()
        assert (stats.size, stats.acquired, stats.in_flight) == (1, 0, 0)
    assert await conn.select_one(f"SELECT count(*) FROM {drop_items}") == (1,)


async def test_pool_replaces_a_connection_cut_mid_transaction(
    proxied_dsn: str,
    fault_proxy: FaultProxy,
    conn: fastpg.Connection,
    drop_items: str,
) -> None:
    async with await fastpg.create_pool(proxied_dsn, min_size=1, max_size=1) as pool:
        async with asyncio.timeout(5):
            with pytest.raises(fastpg.ConnectionClosedError, match=_LOST):
                await _cut_inside_a_transaction(pool, fault_proxy, drop_items)
            assert await pool.select_one("SELECT 1") == (1,)
        await _settle(pool)
        assert pool.stats().size == 1
    assert await conn.select_one(f"SELECT count(*) FROM {drop_items}") == (0,)


async def _cut_inside_a_transaction(pool: fastpg.Pool, proxy: FaultProxy, table: str) -> None:
    async with pool.transaction() as tx:
        await tx.execute(f"INSERT INTO {table} VALUES (1, 'x')")
        proxy.drop(rst=True)
        await tx.select_one("SELECT 1")
