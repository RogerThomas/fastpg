"""Features working together: pool, t-strings and bulk writes, and failure paths across them."""

import asyncio
from typing import TYPE_CHECKING

import msgspec
import pytest

import fastpg
from fastpg import errors

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator


class Item(msgspec.Struct):
    id: int
    v: str


@pytest.fixture(name="items")
async def _items(conn: fastpg.Connection) -> AsyncIterator[str]:
    """A fresh ``integration_items (id int PRIMARY KEY, v text)`` table; yields its name."""
    await conn.execute_script(
        "DROP TABLE IF EXISTS integration_items;"
        " CREATE TABLE integration_items (id int PRIMARY KEY, v text)",
    )
    yield "integration_items"
    await conn.execute_script("DROP TABLE integration_items")


@pytest.fixture(name="pool")
async def _pool(dsn: str) -> AsyncIterator[fastpg.Pool]:
    async with await fastpg.create_pool(dsn, max_size=4) as pool:
        yield pool


class AbortError(Exception):
    """Raised inside a transaction block to make it roll back."""


async def _insert_then_abort(pool: fastpg.Pool, items: str) -> None:
    async with pool.transaction() as tx:
        assert await tx.insert_many(items, [Item(1, "v"), Item(2, "v")]) == 2
        assert await tx.select_one(f"SELECT count(*) FROM {items}") == (2,)  # noqa: S608
        raise AbortError


def _rows(count: int) -> Iterator[tuple[int, str]]:
    return ((i, "v") for i in range(count))


async def test_pool_runs_t_string_queries(pool: fastpg.Pool) -> None:
    item_id = 7
    got = await pool.select_one(t"SELECT {item_id}::int4 AS id, {'v'}::text AS v", into=Item)
    assert got == Item(7, "v")
    assert await pool.select_many(t"SELECT generate_series(1, {3}::int4)") == [(1,), (2,), (3,)]


async def test_pool_t_string_script_with_identifier(pool: fastpg.Pool, items: str) -> None:
    await pool.execute_script(t"INSERT INTO {fastpg.Identifier(items)} VALUES (1, 'v')")
    assert await pool.select_one(t"SELECT count(*) FROM {fastpg.Identifier(items)}") == (1,)


async def test_pool_bulk_writes(pool: fastpg.Pool, items: str) -> None:
    assert await pool.copy_in(items, [Item(1, "copy-in")]) == 1
    assert await pool.insert_many(items, [Item(2, "insert-many")]) == 1
    insert = f"INSERT INTO {items} VALUES ($1, $2)"  # noqa: S608 - fixture table name
    assert await pool.execute_many(insert, [(3, "execute-many")]) == 1
    rows = await pool.select_many(f"SELECT id, v FROM {items} ORDER BY id", into=Item)  # noqa: S608
    assert rows == [Item(1, "copy-in"), Item(2, "insert-many"), Item(3, "execute-many")]
    stats = pool.stats()
    assert stats.in_flight == 0
    assert stats.waiting_acquire == 0


async def test_bulk_write_inside_pool_transaction_rolls_back(pool: fastpg.Pool, items: str) -> None:
    with pytest.raises(AbortError):
        await _insert_then_abort(pool, items)
    assert await pool.select_one(f"SELECT count(*) FROM {items}") == (0,)  # noqa: S608


async def test_copy_in_fails_promptly_when_server_closes(
    conn: fastpg.Connection,
    dsn: str,
    items: str,
) -> None:
    victim = await fastpg.connect(dsn)
    (pid,) = await victim.select_one("SELECT pg_backend_pid()")
    copy = asyncio.create_task(victim.copy_in(items, _rows(50_000_000)))
    await asyncio.sleep(0.3)  # well into streaming
    await conn.select_one("SELECT pg_terminate_backend($1)", pid)
    async with asyncio.timeout(10):  # must fail promptly, never hang
        with pytest.raises((errors.OperationalError, fastpg.ConnectionClosedError)):
            await copy
    await victim.close()
    assert await conn.select_one(f"SELECT count(*) FROM {items}") == (0,)  # noqa: S608
