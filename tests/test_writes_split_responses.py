"""Bulk-write counts when the server's responses reach the client in odd pieces.

xitca resolves a prepare (and the type lookups it makes for user-defined parameter types)
before it has read that response cycle's ReadyForQuery, so the bulk write starts counting
its own responses while that ReadyForQuery may still be on its way. A proxy that delivers
every ReadyForQuery late, in a read of its own, makes that happen every time.
"""

# ruff: noqa: S608 - SQL is built from fixture-generated table names

import asyncio
import struct
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

import pytest

import fastpg


@dataclass(slots=True, eq=False)
class LateReadyProxy:
    """A TCP forwarder that holds back every ReadyForQuery from the server for a moment."""

    _target: tuple[str, int]
    _delay: float = 0.005
    _writers: list[asyncio.StreamWriter] = field(default_factory=list[asyncio.StreamWriter])
    _server: asyncio.Server | None = None

    async def _to_server(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with suppress(OSError):
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        writer.close()

    async def _to_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Forward whole messages, each ReadyForQuery only after a pause (no TLS: framed)."""
        pending = b""
        with suppress(OSError):
            while data := await reader.read(65536):
                pending += data
                while len(pending) >= 5:
                    (length,) = struct.unpack("!i", pending[1:5])
                    if len(pending) < 1 + length:
                        break
                    message, pending = pending[: 1 + length], pending[1 + length :]
                    if message[:1] == b"Z":
                        await writer.drain()
                        await asyncio.sleep(self._delay)
                    writer.write(message)
                    await writer.drain()
        writer.close()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        upstream_reader, upstream_writer = await asyncio.open_connection(*self._target)
        self._writers += [writer, upstream_writer]
        await asyncio.gather(
            self._to_server(reader, upstream_writer),
            self._to_client(upstream_reader, writer),
        )

    async def start(self) -> int:
        """Listen on 127.0.0.1; return the port."""
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self._server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        for writer in self._writers:
            writer.transport.abort()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()


@pytest.fixture
async def late_conn(dsn: str) -> AsyncIterator[fastpg.Connection]:
    """A connection (without TLS) through a `LateReadyProxy` to the test server."""
    parts = urlsplit(dsn)
    proxy = LateReadyProxy((parts.hostname or "localhost", parts.port or 5432))
    port = await proxy.start()
    credentials = parts.netloc.rpartition("@")[0]
    query = f"{parts.query}&sslmode=disable" if parts.query else "sslmode=disable"
    proxied = urlunsplit(parts._replace(netloc=f"{credentials}@127.0.0.1:{port}", query=query))
    try:
        async with await fastpg.connect(proxied) as conn:
            yield conn
    finally:
        await proxy.close()


@pytest.fixture
async def table(conn: fastpg.Connection) -> AsyncIterator[str]:
    await conn.execute_script(
        "DO $$ BEGIN CREATE TYPE split_mood AS ENUM ('sad', 'happy');"
        " EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
        " DROP TABLE IF EXISTS split_responses;"
        " CREATE TABLE split_responses (id int, mood split_mood)",
    )
    yield "split_responses"
    await conn.execute_script("DROP TABLE split_responses")


async def test_insert_many_counts_a_single_full_statement(
    late_conn: fastpg.Connection,
    table: str,
) -> None:
    """64 rows: exactly one grouped INSERT, the only statement of its chunk."""
    assert await late_conn.insert_many(table, [(i,) for i in range(64)], columns=["id"]) == 64
    assert await late_conn.select_one(f"SELECT count(*) FROM {table}") == (64,)


@pytest.mark.parametrize("count", [1, 2000])
async def test_execute_many_counts(late_conn: fastpg.Connection, table: str, count: int) -> None:
    rows = [(i,) for i in range(count)]
    assert await late_conn.execute_many(f"INSERT INTO {table} (id) VALUES ($1)", rows) == count
    assert await late_conn.select_one(f"SELECT count(*) FROM {table}") == (count,)


async def test_user_defined_parameter_types_are_looked_up_first(
    late_conn: fastpg.Connection,
    table: str,
) -> None:
    """The enum's type lookups are response cycles of their own, before the write's."""
    rows = [(1, "sad"), (2, "happy")]
    query = f"INSERT INTO {table} (id, mood) VALUES ($1, $2)"
    assert await late_conn.execute_many(query, rows) == 2
    assert await late_conn.insert_many(table, rows, columns=["id", "mood"]) == 2
    got = await late_conn.select_many(f"SELECT id, mood::text FROM {table} ORDER BY id, mood")
    assert got == [(1, "sad"), (1, "sad"), (2, "happy"), (2, "happy")]
