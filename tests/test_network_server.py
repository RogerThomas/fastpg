"""The server going away: restarted, killed, stopped (on a container of the tests' own), or
ending sessions with pg_terminate_backend.

What must hold: statements in flight fail at once, a connection is closed, and a pool
recovers on its own as soon as the server is back (measured: within a second of
``docker start`` returning, on macOS with Docker Desktop).
"""

import asyncio
import time
from typing import TYPE_CHECKING

import pytest

import fastpg
from fastpg import errors

# Every test here times something: run after the rest, alone (`task test`).
pytestmark = pytest.mark.timing

if TYPE_CHECKING:
    from pathlib import Path

    from faults import DisposableServer

_GONE = (errors.OperationalError, fastpg.ConnectionClosedError)
"""A statement in flight when the server goes: 57P01 (terminated by the server's shutdown or
pg_terminate_backend) or a lost connection (the server process died)."""


async def _settle(pool: fastpg.Pool) -> None:
    """Wait until nothing is in flight, waiting or opening."""
    async with asyncio.timeout(5):
        while (s := pool.stats()).in_flight or s.waiting or s.waiting_acquire or s.opening:
            await asyncio.sleep(0.01)


async def _until_it_answers(pool: fastpg.Pool, seconds: float) -> list[BaseException]:
    """Run a statement on `pool` until it succeeds; return the errors seen meanwhile."""
    seen: list[BaseException] = []
    async with asyncio.timeout(seconds):
        while True:
            try:
                await pool.select_one("SELECT 1")
            except fastpg.Error as e:
                seen.append(e)
                await asyncio.sleep(0.05)
            else:
                return seen


@pytest.mark.timeout(60)
async def test_pool_recovers_from_a_restart(edge: DisposableServer) -> None:
    async with (
        await fastpg.connect(edge.dsn) as conn,
        await fastpg.create_pool(edge.dsn, min_size=2, max_size=4) as pool,
    ):
        busy = asyncio.ensure_future(pool.select_one("SELECT pg_sleep(30)"))
        busy_conn = asyncio.ensure_future(conn.select_one("SELECT pg_sleep(30)"))
        await asyncio.sleep(0.2)
        await edge.restart()
        async with asyncio.timeout(5):
            for statement in (busy, busy_conn):
                with pytest.raises(_GONE):
                    await statement
        assert conn.closed
        with pytest.raises(fastpg.ConnectionClosedError, match="connection lost"):
            await conn.select_one("SELECT 1")
        seen = await _until_it_answers(pool, 20)
        # Meanwhile only connect failures (the server not back yet), never a hang.
        assert all(isinstance(e, fastpg.InterfaceError | errors.OperationalError) for e in seen)
        await _settle(pool)
        stats = pool.stats()
        assert 1 <= stats.size <= 4
        assert (stats.in_flight, stats.acquired) == (0, 0)


@pytest.mark.timeout(60)
async def test_pool_while_the_server_is_down_then_back(edge: DisposableServer) -> None:
    dsn = f"{edge.dsn}?connect_timeout=2"
    async with await fastpg.create_pool(dsn, min_size=2, max_size=4) as pool:
        busy = asyncio.ensure_future(pool.select_one("SELECT pg_sleep(30)"))
        await asyncio.sleep(0.2)
        await edge.kill()  # SIGKILL: no goodbye from the server
        async with asyncio.timeout(5):
            with pytest.raises(fastpg.ConnectionClosedError, match="connection lost"):
                await busy
        # Down: statements fail with their connect, they don't wait for the server. The
        # connect is refused, reset during the TLS negotiation, or (right after the kill,
        # while Docker's port forwarder still accepts) unanswered until connect_timeout.
        for _ in range(3):
            start = time.monotonic()
            with pytest.raises(fastpg.InterfaceError):
                await pool.select_one("SELECT 1")
            assert time.monotonic() - start < 3
        with pytest.raises(fastpg.InterfaceError):
            async with pool.acquire():
                pass
        await edge.start()
        await _until_it_answers(pool, 20)
        await _settle(pool)
        assert pool.stats().size >= 1


async def test_pool_recovers_when_every_idle_session_is_terminated(
    dsn: str,
    conn: fastpg.Connection,
) -> None:
    tagged = f"{dsn}?application_name=fastpg_net_terminate"
    async with await fastpg.create_pool(tagged, min_size=3, max_size=3) as pool:
        await asyncio.gather(*(pool.select_one("SELECT pg_sleep(0.05)") for _ in range(3)))
        query = (
            "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity"
            " WHERE application_name = 'fastpg_net_terminate'"
        )
        assert await conn.select_one(query) == (3,)
        await asyncio.sleep(0.1)  # let the EOFs arrive
        async with asyncio.timeout(5):
            results = await asyncio.gather(*(pool.select_one("SELECT 2") for _ in range(6)))
        assert results == [(2,)] * 6
        await _settle(pool)
        assert pool.stats().size == 3


async def test_pool_replaces_a_terminated_busy_tls_connection(
    tls_server: tuple[str, Path],
) -> None:
    dsn = f"{tls_server[0]}?sslmode=require"
    async with (
        await fastpg.create_pool(dsn, min_size=1, max_size=1) as pool,
        await fastpg.connect(dsn) as killer,
    ):
        (pid,) = await pool.select_one("SELECT pg_backend_pid()")
        sleeping = asyncio.ensure_future(pool.select_one("SELECT pg_sleep(30)"))
        await asyncio.sleep(0.1)
        await killer.select_one("SELECT pg_terminate_backend($1)", pid)
        async with asyncio.timeout(5):
            with pytest.raises(_GONE):
                await sleeping
            assert await pool.select_one("SELECT pg_backend_pid()") != (pid,)
        await _settle(pool)
        assert pool.stats().size == 1
