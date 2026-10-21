"""Fixtures for the CodSpeed benchmarks (``uv run pytest benchmarks --codspeed``).

They run against an existing PostgreSQL seeded with docker/initdb/*.sql: ``FASTPG_DSN``, or
the compose server (``task db-up``). CodSpeed's CPU simulation measures the client process
only, so what's tracked is fastpg's own work per query: encoding, protocol handling and
decoding rows into Python objects. The server's time isn't counted.
"""

import asyncio
import os
from typing import TYPE_CHECKING

import pytest

import fastpg

if TYPE_CHECKING:
    from collections.abc import Coroutine, Iterator
    from typing import Any

DEFAULT_DSN = "postgresql://fastpg:fastpg@127.0.0.1:55432/fastpg"


class Runner:
    """Runs coroutines to completion on one event loop, shared by the session's connection."""

    def __init__(self, loop: asyncio.AbstractEventLoop, conn: fastpg.Connection) -> None:
        self.loop = loop
        self.conn = conn

    def run[T](self, coro: Coroutine[Any, Any, T]) -> T:
        return self.loop.run_until_complete(coro)


@pytest.fixture(scope="session")
def runner() -> Iterator[Runner]:
    """One connection for the whole session; each benchmark warms its statement up first."""
    loop = asyncio.new_event_loop()
    conn = loop.run_until_complete(fastpg.connect(os.environ.get("FASTPG_DSN", DEFAULT_DSN)))
    try:
        yield Runner(loop, conn)
    finally:
        loop.run_until_complete(conn.close())
        loop.close()
