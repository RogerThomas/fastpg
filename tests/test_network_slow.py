"""Slow queries: pg_sleep, lock waits, statement_timeout, and cancelling them with
``asyncio.timeout`` or ``Task.cancel()``.

A statement whose caller gives up is cancelled on the server when it is the only one in flight
on its connection: a CancelRequest on a new connection (16 bytes, or over TLS when the
connection uses TLS). The connection sends nothing more until the server has closed that
connection, so a cancel that arrives late never lands on the next statement. With several
statements pipelined nothing is sent: the cancelled one runs to its end, and the statements
behind it wait for it.
"""

import asyncio
import time
from contextlib import suppress
from typing import TYPE_CHECKING

import pytest
from faults import with_params

import fastpg
from fastpg import errors

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from conftest import LoopErrors
    from faults import FaultProxy


@pytest.fixture
async def locked_row(conn: fastpg.Connection) -> AsyncIterator[str]:
    await conn.execute_script(
        "DROP TABLE IF EXISTS net_slow_items;"
        " CREATE TABLE net_slow_items (id int PRIMARY KEY, v text);"
        " INSERT INTO net_slow_items VALUES (1, 'first')",
    )
    yield "net_slow_items"
    await conn.execute_script("DROP TABLE net_slow_items")


async def _cancel_closed(proxy: FaultProxy) -> None:
    """Wait until the cancel's connection through `proxy` (its second) is closed."""
    async with asyncio.timeout(2):
        while proxy.to_server.ended < 1:
            await asyncio.sleep(0.01)


async def test_a_timed_out_sleep_is_cancelled_on_the_server(conn: fastpg.Connection) -> None:
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.2):
            await conn.select_one("SELECT pg_sleep(5)")
    assert await conn.select_one("SELECT 1") == (1,)  # at once, not when the sleep ends
    assert time.monotonic() - start < 1


async def test_leaving_the_connection_after_a_timed_out_sleep_is_prompt(dsn: str) -> None:
    """Closing waits for the statements in flight: the cancelled one ends at once."""
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        async with await fastpg.connect(dsn) as conn, asyncio.timeout(0.2):
            await conn.select_one("SELECT pg_sleep(5)")
    assert time.monotonic() - start < 1


async def test_a_cancel_is_a_16_byte_request_on_a_connection_of_its_own(
    proxied_dsn: str,
    fault_proxy: FaultProxy,
) -> None:
    async with await fastpg.connect(proxied_dsn) as conn:
        task = asyncio.create_task(conn.select_one("SELECT pg_sleep(5)"))
        await asyncio.sleep(0.1)
        sent = fault_proxy.to_server.forwarded
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await _cancel_closed(fault_proxy)
        assert fault_proxy.accepted == 2
        assert fault_proxy.to_server.forwarded - sent == 16
        async with asyncio.timeout(1):
            assert await conn.select_one("SELECT 1") == (1,)


async def _sleep_in_a_transaction(conn: fastpg.Connection) -> None:
    async with conn.transaction() as tx:
        await tx.execute("SET LOCAL application_name = 'in-transaction'")
        await tx.select_one("SELECT pg_sleep(5)")


async def test_a_timed_out_transaction_rolls_back_at_once(conn: fastpg.Connection) -> None:
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.2):
            await _sleep_in_a_transaction(conn)
    # The ROLLBACK ran first (not after the sleep): this runs outside the transaction.
    assert await conn.select_one("SELECT current_setting('application_name')") != (
        "in-transaction",
    )
    assert time.monotonic() - start < 1


async def test_a_timed_out_statement_on_an_acquired_connection_is_cancelled(dsn: str) -> None:
    async with await fastpg.create_pool(dsn, min_size=1, max_size=1) as pool:
        start = time.monotonic()
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.2), pool.acquire() as conn:
                await conn.select_one("SELECT pg_sleep(5)")
        # The one connection is reset and back in rotation at once.
        assert await pool.select_one("SELECT 1") == (1,)
        assert time.monotonic() - start < 1


async def test_a_timed_out_pool_transaction_rolls_back_at_once(dsn: str) -> None:
    async with await fastpg.create_pool(dsn, min_size=1, max_size=1) as pool:
        start = time.monotonic()
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.2), pool.transaction() as tx:
                await tx.select_one("SELECT pg_sleep(5)")
        assert await pool.select_one("SELECT 1") == (1,)
        assert time.monotonic() - start < 1


async def test_a_timed_out_shared_statement_alone_on_its_connection_is_cancelled(
    dsn: str,
) -> None:
    async with await fastpg.create_pool(dsn, min_size=1, max_size=1) as pool:
        start = time.monotonic()
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.2):
                await pool.select_one("SELECT pg_sleep(5)")
        assert pool.stats().in_flight == 1  # its slot, until the cancel is over
        assert await pool.select_one("SELECT 1") == (1,)  # on the same connection
        async with asyncio.timeout(1):
            while pool.stats().in_flight:
                await asyncio.sleep(0.01)
        assert time.monotonic() - start < 1.5
        assert pool.stats().idle == 1


@pytest.mark.usefixtures("locked_row")
async def test_a_timed_out_write_waiting_for_a_lock_never_commits(
    conn: fastpg.Connection,
    dsn: str,
) -> None:
    """The UPDATE waiting for the row lock is cancelled on the server: it doesn't run, and
    commit, once the lock is released."""
    async with await fastpg.connect(dsn) as writer:
        async with conn.transaction() as tx:
            await tx.select_one("SELECT v FROM net_slow_items WHERE id = 1 FOR UPDATE")
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.2):
                    await writer.execute("UPDATE net_slow_items SET v = 'late' WHERE id = 1")
            # The writer is free while the lock is still held: its UPDATE was cancelled.
            async with asyncio.timeout(1):
                assert await writer.select_one("SELECT 1") == (1,)
        await asyncio.sleep(0.1)
        assert await conn.select_one("SELECT v FROM net_slow_items WHERE id = 1") == ("first",)


@pytest.mark.usefixtures("locked_row")
async def test_a_timed_out_execute_many_waiting_for_a_lock_is_cancelled(
    conn: fastpg.Connection,
    dsn: str,
) -> None:
    rows = [("late",)] * 3000  # more than two chunks: still being fed when cancelled
    async with await fastpg.connect(dsn) as writer:
        async with conn.transaction() as tx:
            await tx.select_one("SELECT v FROM net_slow_items WHERE id = 1 FOR UPDATE")
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.2):
                    await writer.execute_many(
                        "UPDATE net_slow_items SET v = $1 WHERE id = 1",
                        rows,
                    )
            async with asyncio.timeout(1):
                assert await writer.select_one("SELECT 1") == (1,)  # rolled back already
        assert await conn.select_one("SELECT v FROM net_slow_items WHERE id = 1") == ("first",)


@pytest.mark.usefixtures("locked_row")
async def test_a_timed_out_copy_waiting_for_a_lock_is_cancelled(
    conn: fastpg.Connection,
    dsn: str,
) -> None:
    """The COPY's first row waits for an open transaction that inserted the same key."""
    value = "x" * 1000
    # 30 MB: more than the socket buffers and the write buffer take while the COPY waits.
    rows = [(2, value), *((i, value) for i in range(3, 30_000))]
    async with await fastpg.connect(dsn) as writer:
        async with conn.transaction() as tx:
            await tx.execute("INSERT INTO net_slow_items VALUES (2, 'held')")
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.5):
                    await writer.copy_in("net_slow_items", rows)
            async with asyncio.timeout(2):
                assert await writer.select_one("SELECT 1") == (1,)
        assert await conn.select_one("SELECT count(*) FROM net_slow_items") == (2,)


async def test_a_cancelled_statement_among_several_in_flight_cancels_nothing(
    proxied_dsn: str,
    fault_proxy: FaultProxy,
) -> None:
    """The server would cancel whatever runs, maybe another caller's statement: no cancel is
    sent, and the cancelled statement runs to its end."""
    query = "SELECT $1::int FROM pg_sleep(0.3)"
    async with await fastpg.connect(proxied_dsn) as conn:
        tasks = [asyncio.create_task(conn.select_one(query, i)) for i in range(3)]
        await asyncio.sleep(0.1)
        tasks[1].cancel()
        assert await tasks[0] == (0,)
        assert await tasks[2] == (2,)
        assert tasks[1].cancelled()
    assert fault_proxy.accepted == 1


async def test_a_cancelled_shared_statement_among_several_cancels_nothing(
    proxied_dsn: str,
    fault_proxy: FaultProxy,
) -> None:
    query = "SELECT $1::int FROM pg_sleep(0.3)"
    async with await fastpg.create_pool(proxied_dsn, min_size=1, max_size=1) as pool:
        tasks = [asyncio.create_task(pool.select_one(query, i)) for i in range(3)]
        await asyncio.sleep(0.1)
        tasks[0].cancel()
        assert await tasks[1] == (1,)
        assert await tasks[2] == (2,)
        assert tasks[0].cancelled()
    assert fault_proxy.accepted == 1


async def test_a_late_cancel_never_lands_on_the_next_statement(
    proxied_dsn: str,
    fault_proxy: FaultProxy,
) -> None:
    """Statements given up on right as they finish: their cancel may reach the server after
    they ended. The statement after each (2 ms on the server) is never the one cancelled."""
    async with await fastpg.connect(proxied_dsn) as conn:
        for i in range(200):
            with suppress(TimeoutError):
                async with asyncio.timeout((i % 40) / 10_000):  # 0 to 4 ms
                    await conn.select_one("SELECT 1 FROM pg_sleep(0.001)")
            assert await conn.select_one("SELECT 2 FROM pg_sleep(0.002)") == (2,)
    assert fault_proxy.accepted > 50  # many were sent


async def test_a_cancel_uses_tls_when_the_connection_does(
    tls_proxied_dsn: str,
    tls_fault_proxy: FaultProxy,
) -> None:
    async with await fastpg.connect(tls_proxied_dsn) as conn:
        task = asyncio.create_task(conn.select_one("SELECT pg_sleep(5)"))
        await asyncio.sleep(0.1)
        sent = tls_fault_proxy.to_server.forwarded
        start = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await _cancel_closed(tls_fault_proxy)
        # An SSLRequest and a TLS handshake, not a bare 16-byte request.
        assert tls_fault_proxy.to_server.forwarded - sent > 100
        assert await conn.select_one("SELECT 1") == (1,)
        assert time.monotonic() - start < 1


async def test_a_cancel_over_a_unix_socket(unix_dsn: str) -> None:
    async with await fastpg.connect(unix_dsn) as conn:
        start = time.monotonic()
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.2):
                await conn.select_one("SELECT pg_sleep(5)")
        assert await conn.select_one("SELECT 1") == (1,)
        assert time.monotonic() - start < 1


async def test_a_cancel_to_an_unreachable_address_gives_up_after_connect_timeout(
    proxied_dsn: str,
    fault_proxy: FaultProxy,
    loop_errors: LoopErrors,
) -> None:
    """Nothing reached the server: the statement runs to its end, and the next one waits for
    the cancel's connect to give up (2 s), not for the OS (a minute or more)."""
    async with await fastpg.connect(with_params(proxied_dsn, connect_timeout="2")) as conn:
        fault_proxy.unreachable()
        start = time.monotonic()
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.2):
                await conn.select_one("SELECT pg_sleep(1)")
        assert time.monotonic() - start < 0.5
        assert await conn.select_one("SELECT 1") == (1,)
        assert 1.9 < time.monotonic() - start < 3
    assert loop_errors.messages() == []


async def test_a_terminated_connection_sends_no_cancel(
    proxied_dsn: str,
    fault_proxy: FaultProxy,
) -> None:
    conn = await fastpg.connect(proxied_dsn)
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.1):
            await conn.select_one("SELECT pg_sleep(5)")
    conn.terminate()  # before the cancel's connection is opened
    await asyncio.sleep(0.2)
    assert fault_proxy.accepted == 1


async def test_statement_timeout_from_the_dsn(dsn: str) -> None:
    capped = with_params(dsn, options="-c%20statement_timeout%3D100")
    async with await fastpg.connect(capped) as conn:
        start = time.monotonic()
        with pytest.raises(errors.QueryCanceledError) as exc:
            await conn.select_one("SELECT pg_sleep(2)")
        assert exc.value.sqlstate == "57014"
        assert time.monotonic() - start < 1
        assert await conn.select_one("SELECT 1") == (1,)  # usable
    async with await fastpg.create_pool(capped, min_size=1, max_size=1) as pool:
        (pid,) = await pool.select_one("SELECT pg_backend_pid()")
        with pytest.raises(errors.QueryCanceledError):
            await pool.select_one("SELECT pg_sleep(2)")
        assert await pool.select_one("SELECT pg_backend_pid()") == (pid,)  # kept, not replaced


async def _fast_queries(pool: fastpg.Pool, count: int) -> float:
    start = time.monotonic()
    for i in range(count):
        assert await pool.select_one("SELECT $1::int", i) == (i,)
    return time.monotonic() - start


async def test_a_slow_query_cancelled_among_others_does_not_hold_up_the_pool(dsn: str) -> None:
    """Pipelined with another statement, the cancelled one isn't cancelled on the server: it
    keeps its slot, and fast statements go to the other connection meanwhile."""
    async with await fastpg.create_pool(dsn, min_size=2, max_size=2) as pool:
        # Both on one connection: the second is pipelined behind the first.
        async with pool.acquire():
            slow = asyncio.create_task(pool.select_one("SELECT pg_sleep(1)"))
            other = asyncio.create_task(pool.select_one("SELECT 1, pg_sleep(1)"))
            await asyncio.sleep(0.1)
        slow.cancel()
        with pytest.raises(asyncio.CancelledError):
            await slow
        assert pool.stats().in_flight == 2  # still running on the server
        # Every fast query goes to the other connection meanwhile.
        assert await _fast_queries(pool, 20) < 0.5
        assert (await other)[0] == 1
        async with asyncio.timeout(3):
            while pool.stats().in_flight:
                await asyncio.sleep(0.01)
        assert pool.stats().idle == 2
