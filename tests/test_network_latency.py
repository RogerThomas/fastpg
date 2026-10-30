"""A slow network (latency added each way by a `FaultProxy`) and a fragmenting one (bytes
delivered a few at a time).

What must hold: results are correct, pipelining keeps a batch to a few round trips rather
than one per statement, and nothing times out on its own (fastpg has no internal timeouts).
"""

# ruff: noqa: S608 - SQL is built from fixture-generated table names

import asyncio
import time
from collections.abc import AsyncIterator

import msgspec
import pytest
from faults import FaultProxy

import fastpg

# Every test here times something: run after the rest, alone (`task test`).
pytestmark = pytest.mark.timing


class Row(msgspec.Struct):
    n: int
    s: str


@pytest.fixture
async def net_items(conn: fastpg.Connection) -> AsyncIterator[str]:
    await conn.execute_script(
        "DROP TABLE IF EXISTS net_latency_items; CREATE TABLE net_latency_items (id int, v text)",
    )
    yield "net_latency_items"
    await conn.execute_script("DROP TABLE net_latency_items")


@pytest.mark.parametrize("rtt", [0.05, 0.2])
async def test_queries_cost_one_round_trip(
    proxied_dsn: str,
    fault_proxy: FaultProxy,
    rtt: float,
) -> None:
    fault_proxy.to_server.delay = fault_proxy.to_client.delay = rtt / 2
    async with await fastpg.connect(proxied_dsn) as conn:
        trips = fault_proxy.round_trips
        assert await conn.select_one("SELECT $1::int", 1) == (1,)
        assert fault_proxy.round_trips - trips == 2  # prepares first
        trips = fault_proxy.round_trips
        start = time.monotonic()
        assert await conn.select_one("SELECT $1::int", 2) == (2,)
        assert time.monotonic() - start >= rtt
        assert fault_proxy.round_trips - trips == 1
        rows = await conn.select_many("SELECT n, n::text FROM generate_series(1, 5000) AS n")
        assert rows[-1] == (5000, "5000")
        assert len(rows) == 5000
        assert await conn.execute("SELECT generate_series(1, 3)") == 3


async def test_pipelined_queries_share_round_trips(
    proxied_dsn: str,
    fault_proxy: FaultProxy,
) -> None:
    rtt = 0.1
    fault_proxy.to_server.delay = fault_proxy.to_client.delay = rtt / 2
    async with await fastpg.connect(proxied_dsn) as conn:
        await conn.select_one("SELECT $1::int", 0)
        trips = fault_proxy.round_trips
        results = await asyncio.gather(*(conn.select_one("SELECT $1::int", i) for i in range(200)))
        assert results == [(i,) for i in range(200)]
        assert fault_proxy.round_trips - trips == 1  # 200 statements, one round trip


async def test_pool_under_latency(proxied_dsn: str, fault_proxy: FaultProxy) -> None:
    """The pool grows while the latency makes every statement slow (each new connection
    costs several round trips), pipelining onto the busy ones meanwhile."""
    rtt = 0.1
    fault_proxy.to_server.delay = fault_proxy.to_client.delay = rtt / 2
    async with await fastpg.create_pool(proxied_dsn, min_size=1, max_size=4) as pool:
        start = time.monotonic()
        queries = [pool.select_one("SELECT $1::int", i, into=dict) for i in range(400)]
        results = await asyncio.gather(*queries)
        assert results == [{"int4": i} for i in range(400)]
        # At most 4 x 8 statements in flight: 13 round trips at best, plus the growth.
        assert time.monotonic() - start < 30 * rtt
        assert 1 <= pool.stats().size <= 4


async def test_transaction_and_bulk_writes_under_latency(
    proxied_dsn: str,
    fault_proxy: FaultProxy,
    conn: fastpg.Connection,
    net_items: str,
) -> None:
    rtt = 0.1
    fault_proxy.to_server.delay = fault_proxy.to_client.delay = rtt / 2
    async with await fastpg.create_pool(proxied_dsn, min_size=1, max_size=2) as pool:
        async with pool.transaction() as tx:
            await tx.execute(f"INSERT INTO {net_items} VALUES ($1, $2)", 1, "tx")
            await tx.execute(f"INSERT INTO {net_items} VALUES ($1, $2)", 2, "tx")
        start = time.monotonic()
        rows = [(i, "copy") for i in range(3, 30_003)]
        assert await pool.copy_in(net_items, rows) == 30_000
        query = f"INSERT INTO {net_items} VALUES ($1, $2)"
        assert await pool.execute_many(query, [(i, "many") for i in range(5000)]) == 5000
        inserted = await pool.insert_many(net_items, [(i, "insert") for i in range(5000)])
        assert inserted == 5000
        # A chunk at a time with two in flight: a few round trips each, not one per row.
        assert time.monotonic() - start < 40 * rtt
    query = f"SELECT v, count(*) FROM {net_items} GROUP BY v ORDER BY v"
    counts = [("copy", 30_000), ("insert", 5000), ("many", 5000), ("tx", 2)]
    assert await conn.select_many(query) == counts


@pytest.mark.timeout(60)
async def test_a_one_second_round_trip_is_only_slow(
    proxied_dsn: str,
    fault_proxy: FaultProxy,
) -> None:
    """About 10 s in all (the startup alone is several round trips): nothing gives up."""
    fault_proxy.to_server.delay = fault_proxy.to_client.delay = 0.5
    async with await fastpg.create_pool(proxied_dsn, min_size=1, max_size=1) as pool:
        assert await pool.select_one("SELECT 'slow'") == ("slow",)
        async with pool.transaction() as tx:
            assert await tx.select_one("SELECT 1") == (1,)


# --- fragmented delivery ----------------------------------------------------------------------


async def _exercise(conn: fastpg.Connection, table: str) -> None:
    """Statements whose responses cover every message the client parses."""
    one = await conn.select_one("SELECT 1, 'a', NULL::int, 2.5::float8, true")
    assert one == (1, "a", None, 2.5, True)
    rows = await conn.select_many("SELECT n, repeat('é', n % 5) FROM generate_series(1, 600) n")
    assert rows == [(n, "é" * (n % 5)) for n in range(1, 601)]  # two streamed chunks
    query = "SELECT n, n::text AS s FROM generate_series(1, 3) n"
    structs = await conn.select_many(query, into=Row)
    assert structs == [Row(1, "1"), Row(2, "2"), Row(3, "3")]
    assert await conn.select_one("SELECT repeat('x', 20000)") == ("x" * 20000,)
    with pytest.raises(fastpg.DataError):
        await conn.select_one("SELECT 1 / 0")
    results = await asyncio.gather(*(conn.select_one("SELECT $1::int", i) for i in range(20)))
    assert results == [(i,) for i in range(20)]
    query = f"INSERT INTO {table} VALUES ($1, $2)"
    assert await conn.execute_many(query, [(i, "many") for i in range(100)]) == 100
    assert await conn.copy_in(table, [(i, "copy") for i in range(100)]) == 100
    async with conn.transaction() as tx:
        await tx.execute(f"INSERT INTO {table} VALUES (1, 'tx')")
    assert await conn.select_one(f"SELECT count(*) FROM {table}") == (201,)


@pytest.mark.timeout(60)
@pytest.mark.parametrize("size", [1, 7])
async def test_responses_in_tiny_fragments(
    proxied_dsn: str,
    fault_proxy: FaultProxy,
    net_items: str,
    size: int,
) -> None:
    fault_proxy.to_client.fragment = fault_proxy.to_server.fragment = size
    async with await fastpg.connect(proxied_dsn) as conn:  # the startup is fragmented too
        await _exercise(conn, net_items)


@pytest.mark.timeout(60)
async def test_tls_records_in_one_byte_fragments(
    tls_proxied_dsn: str,
    tls_fault_proxy: FaultProxy,
) -> None:
    """Every TLS record arrives a byte at a time: rustls must reassemble them."""
    tls_fault_proxy.to_client.fragment = tls_fault_proxy.to_server.fragment = 1
    async with await fastpg.connect(tls_proxied_dsn) as conn:
        query = "SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()"
        assert await conn.select_one(query) == (True,)
        rows = await conn.select_many("SELECT n FROM generate_series(1, 600) n")
        assert rows == [(n,) for n in range(1, 601)]
