"""Transactions at the edges: errors inside, failed commits, nesting, misuse of the object."""

import asyncio
from typing import TYPE_CHECKING

import pytest

import fastpg
from fastpg import errors

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class BlockError(Exception):
    """Raised inside a transaction block."""


@pytest.fixture
async def table(conn: fastpg.Connection) -> AsyncIterator[None]:
    """A fresh ``edge_tx`` table whose ``id`` is unique, checked at COMMIT (deferred)."""
    await conn.execute_script(
        "DROP TABLE IF EXISTS edge_tx;"
        " CREATE TABLE edge_tx (id int UNIQUE DEFERRABLE INITIALLY DEFERRED, v text)",
    )
    yield
    await conn.execute_script("DROP TABLE edge_tx")


async def _ids(conn: fastpg.Connection) -> list[int]:
    rows = await conn.select_many("SELECT id FROM edge_tx ORDER BY id")
    return [row[0] for row in rows]


async def _in_transaction(conn: fastpg.Connection) -> bool:
    """Whether ``conn``'s session is inside a transaction block."""
    # A simple-protocol statement outside a block starts its own transaction: now() (the
    # transaction's start) is then exactly its statement_timestamp().
    try:
        await conn.execute_script(
            "SELECT (CASE WHEN now() = statement_timestamp() THEN '0' ELSE 'in a block' END)::int",
        )
    except errors.DataError:
        return True
    return False


async def _insert_and_raise(conn: fastpg.Connection) -> None:
    async with conn.transaction() as tx:
        await tx.execute("INSERT INTO edge_tx VALUES (1, 'a')")
        raise BlockError


async def _pool_insert_and_raise(pool: fastpg.Pool) -> None:
    async with pool.transaction() as tx:
        await tx.execute_script("SET application_name = 'edge-tx'; SET work_mem = '7MB'")
        await tx.execute("INSERT INTO edge_tx VALUES (1, 'a')")
        raise BlockError


async def _leak_transaction(pool: fastpg.Pool) -> fastpg.Transaction:
    async with pool.transaction() as tx:
        await tx.execute("INSERT INTO edge_tx VALUES (1, 'a')")
    return tx


async def _other_task_inserts(conn: fastpg.Connection) -> int:
    return await conn.execute("INSERT INTO edge_tx VALUES (2, 'from another task')")


async def _go_on_after_an_error(conn: fastpg.Connection) -> None:
    """Swallow a server error inside the block, try another query, then exit normally."""
    async with conn.transaction() as tx:
        await tx.execute("INSERT INTO edge_tx VALUES (1, 'a')")
        with pytest.raises(errors.DataError, match="division by zero"):
            await tx.select_one("SELECT 1 / 0")
        with pytest.raises(errors.DatabaseError, match="current transaction is aborted") as exc:
            await tx.select_one("SELECT 1")
        assert exc.value.sqlstate == "25P02"


async def _insert_duplicates(conn: fastpg.Connection) -> None:
    async with conn.transaction() as tx:
        await tx.execute("INSERT INTO edge_tx VALUES (1, 'a'), (1, 'b')")  # checked at COMMIT


async def _raise_in_savepoints(tx: fastpg.Transaction) -> None:
    async with tx.savepoint():
        await tx.execute("INSERT INTO edge_tx VALUES (3, 'second')")
        async with tx.savepoint():
            await tx.execute("INSERT INTO edge_tx VALUES (4, 'third')")
        raise BlockError


async def _fail_in_savepoint(tx: fastpg.Transaction) -> None:
    async with tx.savepoint():
        await tx.select_one("SELECT 1 / 0")


async def _enter(tx: fastpg.Transaction) -> None:
    async with tx:
        pass


async def _enter_savepoint(tx: fastpg.Transaction) -> None:
    async with tx.savepoint():
        pass


async def _use_stale_inside_another_transaction(
    pool: fastpg.Pool,
    stale: fastpg.Transaction,
) -> None:
    async with pool.transaction() as tx:
        await tx.execute("INSERT INTO edge_tx VALUES (10, 'other task')")
        with pytest.raises(fastpg.InterfaceError, match="transaction is not active"):
            await stale.execute("INSERT INTO edge_tx VALUES (2, 'stale')")
        raise BlockError


async def _share_and_raise(conn: fastpg.Connection) -> None:
    async with conn.transaction() as tx:
        await tx.execute("INSERT INTO edge_tx VALUES (1, 'a')")
        assert await asyncio.create_task(_other_task_inserts(conn)) == 1
        assert await conn.select_one("SELECT count(*)::int FROM edge_tx") == (2,)
        raise BlockError


@pytest.mark.usefixtures("table")
async def test_an_exception_in_the_block_rolls_back(conn: fastpg.Connection) -> None:
    with pytest.raises(BlockError):
        await _insert_and_raise(conn)
    assert await _ids(conn) == []
    assert not await _in_transaction(conn)


@pytest.mark.usefixtures("table")
async def test_queries_after_a_server_error_inside_fail_with_25p02(
    conn: fastpg.Connection,
) -> None:
    with pytest.raises(errors.DatabaseError) as exc:
        await _go_on_after_an_error(conn)
    # The block went on after the error and exited normally: COMMIT would have been a silent
    # ROLLBACK, so it raises instead.
    assert exc.value.sqlstate == "25P02"
    assert exc.value.__notes__ == [
        "fastpg: the transaction was aborted by an earlier error; rolled back",
    ]
    assert await _ids(conn) == []
    assert not await _in_transaction(conn)


@pytest.mark.usefixtures("table")
async def test_a_failing_commit_surfaces_and_leaves_the_connection_usable(
    conn: fastpg.Connection,
) -> None:
    with pytest.raises(errors.UniqueViolationError) as exc:
        await _insert_duplicates(conn)
    assert exc.value.sqlstate == "23505"
    assert await _ids(conn) == []
    assert not await _in_transaction(conn)
    async with conn.transaction() as tx:
        await tx.execute("INSERT INTO edge_tx VALUES (1, 'a')")
    assert await _ids(conn) == [1]


@pytest.mark.usefixtures("table")
async def test_nested_savepoints_with_the_inner_one_rolled_back(conn: fastpg.Connection) -> None:
    async with conn.transaction() as tx:
        await tx.execute("INSERT INTO edge_tx VALUES (1, 'outer')")
        async with tx.savepoint():
            await tx.execute("INSERT INTO edge_tx VALUES (2, 'first')")
            with pytest.raises(BlockError):
                await _raise_in_savepoints(tx)
            await tx.execute("INSERT INTO edge_tx VALUES (5, 'first again')")
        # A server error inside a savepoint is contained by it.
        with pytest.raises(errors.DataError):
            await _fail_in_savepoint(tx)
        await tx.execute("INSERT INTO edge_tx VALUES (6, 'outer again')")
    assert await _ids(conn) == [1, 2, 5, 6]


async def test_a_second_transaction_while_one_is_active_is_refused(
    conn: fastpg.Connection,
) -> None:
    async with conn.transaction() as tx:
        with pytest.raises(fastpg.InterfaceError, match="already active"):
            conn.transaction()
        await tx.select_one("SELECT 1")
    async with conn.transaction() as tx:  # fine once the first one is over
        await tx.select_one("SELECT 1")


async def test_a_transaction_object_is_entered_once(conn: fastpg.Connection) -> None:
    tx = conn.transaction()
    await _enter(tx)
    with pytest.raises(fastpg.InterfaceError, match="only be entered once"):
        await _enter(tx)
    with pytest.raises(fastpg.InterfaceError, match="needs an active transaction"):
        await _enter_savepoint(tx)


@pytest.mark.usefixtures("table")
async def test_queries_through_a_finished_transaction_fail(conn: fastpg.Connection) -> None:
    async with conn.transaction() as tx:
        await tx.execute("INSERT INTO edge_tx VALUES (1, 'a')")
    # Running them outside the transaction (autocommit) would silently change their meaning.
    with pytest.raises(fastpg.InterfaceError, match="transaction is not active"):
        await tx.execute("INSERT INTO edge_tx VALUES (2, 'b')")
    with pytest.raises(fastpg.InterfaceError, match="transaction is not active"):
        await tx.select_one("SELECT 1")
    with pytest.raises(fastpg.InterfaceError, match="transaction is not active"):
        await tx.execute_script("SELECT 1")
    with pytest.raises(fastpg.InterfaceError, match="transaction is not active"):
        await tx.copy_in("edge_tx", [(3, "c")])
    assert await _ids(conn) == [1]
    assert await conn.select_one("SELECT 1") == (1,)  # the connection itself is fine


async def test_queries_through_a_transaction_never_entered_fail(conn: fastpg.Connection) -> None:
    tx = conn.transaction()
    with pytest.raises(fastpg.InterfaceError, match="transaction is not active"):
        await tx.select_one("SELECT 1")


@pytest.mark.usefixtures("table")
async def test_a_finished_pool_transaction_cannot_reach_its_connection_again(
    dsn: str,
    conn: fastpg.Connection,
) -> None:
    async with await fastpg.create_pool(dsn, min_size=1, max_size=1) as pool:
        stale = await _leak_transaction(pool)
        # The connection is back in the pool, and another transaction now runs on it: the
        # stale one's statement must not land in it.
        with pytest.raises(BlockError):
            await _use_stale_inside_another_transaction(pool, stale)
    assert await _ids(conn) == [1]


@pytest.mark.usefixtures("table")
async def test_pool_transaction_that_raises_leaves_the_session_clean(dsn: str) -> None:
    async with await fastpg.create_pool(dsn, min_size=1, max_size=1) as pool:
        with pytest.raises(BlockError):
            await _pool_insert_and_raise(pool)
        assert await pool.select_one("SELECT count(*)::int FROM edge_tx") == (0,)
        settings = "SELECT current_setting('application_name'), current_setting('work_mem')"
        name, work_mem = await pool.select_one(settings)
        assert (name == "edge-tx", work_mem == "7MB") == (False, False)
        async with pool.acquire() as acquired:
            assert not await _in_transaction(acquired)
        assert pool.stats().size == 1  # reset, not replaced


@pytest.mark.usefixtures("table")
async def test_other_tasks_queries_on_the_connection_run_inside_its_transaction(
    conn: fastpg.Connection,
) -> None:
    # As Connection.transaction documents: the connection is still shared while the block
    # runs, so another task's statement through it joins the transaction (and its rollback).
    with pytest.raises(BlockError):
        await _share_and_raise(conn)
    assert await _ids(conn) == []
