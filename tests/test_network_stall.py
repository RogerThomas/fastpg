"""A server or network that stops responding (a `FaultProxy` stall, or a paused container).

What must hold: ``asyncio.timeout`` around any call returns on time; once the server answers
again, every connection and pool slot is usable or replaced; nothing is left open.
"""

import asyncio

import pytest
from faults import FaultProxy

import fastpg


async def test_timed_out_query_leaves_the_connection_usable(
    proxied_dsn: str,
    fault_proxy: FaultProxy,
) -> None:
    async with await fastpg.connect(proxied_dsn) as conn:
        fault_proxy.stall()
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.2):
                await conn.select_one("SELECT 1")
        assert not conn.closed
        fault_proxy.resume()
        async with asyncio.timeout(5):
            # Its own result, not the abandoned query's (which arrives first).
            assert await conn.select_one("SELECT 2") == (2,)


async def test_close_completes_after_an_earlier_close_timed_out(
    proxied_dsn: str,
    fault_proxy: FaultProxy,
) -> None:
    conn = await fastpg.connect(proxied_dsn)
    fault_proxy.stall()
    # Close is graceful: it waits for the server to end the session.
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.2):
            await conn.close()
    fault_proxy.resume()
    async with asyncio.timeout(5):
        await conn.close()
    assert conn.closed
