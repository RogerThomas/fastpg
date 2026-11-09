"""Through pgbouncer in transaction mode, with prepared statements on (max_prepared_statements):
clients take turns on 2 server connections, and pgbouncer prepares each named statement on
whichever one runs it."""

# ruff: noqa: S608 - SQL is built from fixture-generated table names

import asyncio
import secrets
from collections.abc import AsyncIterator
from typing import LiteralString, cast

import msgspec
import pytest

import fastpg


class Item(msgspec.Struct):
    id: int
    name: str


@pytest.fixture
async def table(pgbouncer_dsn: str) -> AsyncIterator[LiteralString]:
    """A fresh `(id int PRIMARY KEY, name text)` table (not a temporary one: the session
    changes between transactions)."""
    name = cast("LiteralString", f"pgb_{secrets.token_hex(4)}")
    async with await fastpg.connect(pgbouncer_dsn) as conn:
        await conn.execute_script(f"CREATE TABLE {name} (id int PRIMARY KEY, name text)")
        yield name
        await conn.execute_script(f"DROP TABLE {name}")


async def test_connections_share_two_server_connections(pgbouncer_dsn: str) -> None:
    pids: set[int] = set()

    async def client(n: int) -> None:
        async with await fastpg.connect(pgbouncer_dsn) as conn:
            for i in range(20):
                k = n * 1000 + i
                assert await conn.select_one("SELECT $1::int + 1", k) == (k + 1,)
                assert await conn.select_one("SELECT $1::text || 'x'", str(k)) == (f"{k}x",)
                pids.add((await conn.select_one("SELECT pg_backend_pid()"))[0])

    await asyncio.gather(*(client(n) for n in range(12)))
    assert 1 <= len(pids) <= 2


async def test_queries_sent_together_on_one_connection(pgbouncer_dsn: str) -> None:
    queries: list[LiteralString] = ["SELECT $1::int + 1", "SELECT $1::int * 3", "SELECT $1::text"]
    async with await fastpg.connect(pgbouncer_dsn) as conn:
        for start in range(0, 500, 100):
            ks = range(start, start + 64)
            got = await asyncio.gather(
                *(conn.select_one(queries[k % 3], str(k) if k % 3 == 2 else k) for k in ks),
            )
            assert got == [((k + 1,), (k * 3,), (str(k),))[k % 3] for k in ks]


async def _insert_then_fail(conn: fastpg.Connection, table: LiteralString, row_id: int) -> None:
    async with conn.transaction() as tx:
        await tx.execute(f"INSERT INTO {table} VALUES ($1, 'r')", row_id)
        await tx.select_one("SELECT 1 / 0")


async def test_transactions(pgbouncer_dsn: str, table: LiteralString) -> None:
    async def client(n: int) -> None:
        async with await fastpg.connect(pgbouncer_dsn) as conn:
            for i in range(10):
                async with conn.transaction() as tx:
                    await tx.execute(f"INSERT INTO {table} VALUES ($1, 'c')", n * 100 + i)
                    pid = (await tx.select_one("SELECT pg_backend_pid()"))[0]
                    got = await tx.select_one(
                        f"SELECT count(*) FROM {table} WHERE id / 100 = $1",
                        n,
                    )
                    assert got == (i + 1,)
                    assert await tx.select_one("SELECT pg_backend_pid()") == (pid,)
                with pytest.raises(fastpg.DatabaseError, match="division by zero"):
                    await _insert_then_fail(conn, table, n * 100 + 50 + i)

    await asyncio.gather(*(client(n) for n in range(4)))
    async with await fastpg.connect(pgbouncer_dsn) as conn:
        assert await conn.select_many(f"SELECT name, count(*) FROM {table} GROUP BY name") == [
            ("c", 40),
        ]


async def test_bulk_writes(pgbouncer_dsn: str, table: LiteralString) -> None:
    async with await fastpg.connect(pgbouncer_dsn) as conn:
        rows = [(i, "e") for i in range(1000)]
        assert await conn.execute_many(f"INSERT INTO {table} VALUES ($1, $2)", rows) == 1000
        items = [Item(1000 + i, "i") for i in range(1000)]
        assert await conn.insert_many(table, items) == 1000
        assert await conn.copy_in(table, [(2000 + i, "c") for i in range(1000)]) == 1000
        async with conn.transaction() as tx:
            with pytest.raises(fastpg.UniqueViolationError) as exc:
                await tx.copy_in(table, [(3000, "x"), (0, "duplicate")])
            assert exc.value.row_index == 1
            assert await tx.copy_in(table, [(3001, "t")]) == 1
        assert await conn.select_many(
            f"SELECT name, count(*) FROM {table} GROUP BY name ORDER BY name",
        ) == [("c", 1000), ("e", 1000), ("i", 1000), ("t", 1)]


async def test_evicted_statements(pgbouncer_dsn: str) -> None:
    """With room for one statement, each query closes the one before it."""

    async def client(n: int) -> None:
        async with await fastpg.connect(pgbouncer_dsn, statement_cache_size=1) as conn:
            for i in range(20):
                k = n * 1000 + i
                got = await asyncio.gather(
                    conn.select_one("SELECT $1::int + 1", k),
                    conn.select_one("SELECT $1::int + 2", k),
                    conn.select_one("SELECT $1::text", str(k)),
                    conn.select_one("SELECT $1::int + 1", k),
                )
                assert got == [(k + 1,), (k + 2,), (str(k),), (k + 1,)]
                assert await conn.select_one("SELECT $1::int + 3", k) == (k + 3,)
            assert conn.cached_statements == 1

    await asyncio.gather(*(client(n) for n in range(6)))


async def test_pool(pgbouncer_dsn: str, table: LiteralString) -> None:
    async with await fastpg.create_pool(pgbouncer_dsn, min_size=2, max_size=6) as pool:
        got = await asyncio.gather(
            *(
                pool.select_one("SELECT $1::int + 1" if k % 2 else "SELECT $1::int * 2", k)
                for k in range(500)
            ),
        )
        assert got == [((k + 1) if k % 2 else (k * 2),) for k in range(500)]
        async with pool.transaction() as tx:
            await tx.execute(f"INSERT INTO {table} VALUES (1, 'tx')")
        assert await pool.copy_in(table, [(2, "copy")]) == 1
        assert await pool.select_many(f"SELECT id, name FROM {table} ORDER BY id") == [
            (1, "tx"),
            (2, "copy"),
        ]
