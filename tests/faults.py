"""Network fault injection for the network edge-case tests (``test_network_*.py``).

* `FaultProxy`: a TCP (or unix socket to TCP) forwarder between fastpg and a server that
  adds latency, stalls, drops connections (FIN or RST) and delivers bytes in tiny fragments.
* `DisposableServer`: a PostgreSQL container of the tests' own, to pause, kill and restart.
"""

import asyncio
import contextlib
import socket
import struct
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

from testcontainers.core.container import DockerContainer

import fastpg

if TYPE_CHECKING:
    from collections.abc import Generator


def with_params(dsn: str, **params: str) -> str:
    """URL `dsn` with extra query parameters."""
    query = "&".join(f"{key}={value}" for key, value in params.items())
    return f"{dsn}{'&' if '?' in dsn else '?'}{query}"


def at_address(dsn: str, host: str, port: int) -> str:
    """URL `dsn` pointed at `host:port`, credentials kept."""
    parts = urlsplit(dsn)
    credentials = parts.netloc.rpartition("@")[0]
    return urlunsplit(parts._replace(netloc=f"{credentials}@{host}:{port}"))


def _drop(writer: asyncio.StreamWriter, *, rst: bool) -> None:
    """Close one side of a link at once: with RST (``SO_LINGER`` 0) or with FIN."""
    if writer.transport.is_closing():
        return
    if rst:
        sock = writer.get_extra_info("socket")
        # EINVAL on macOS once the peer has reset the connection: nothing left to reset.
        with contextlib.suppress(OSError):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    writer.transport.abort()


@dataclass(slots=True, eq=False)
class Faults:
    """What a `FaultProxy` does to one direction (to the server or to the client) of every
    connection through it. Changes apply at once, to open connections too."""

    delay: float = 0.0
    """Seconds each read is held before it's forwarded: latency, not a throughput limit."""
    fragment: int = 0
    """Forward at most this many bytes per write, each sent on its own (0: as read)."""
    forwarded: int = 0
    """Bytes forwarded in this direction so far, over every connection."""
    ended: int = 0
    """Connections whose sending side closed (EOF or error), e.g. `to_server.ended` counts
    connections the client closed."""
    _flowing: asyncio.Event = field(default_factory=asyncio.Event)
    _budget: int | None = None
    _rst: bool = False

    def __post_init__(self) -> None:
        self._flowing.set()

    def stall(self) -> None:
        """Stop forwarding: bytes read are held (not dropped) until `resume()`."""
        self._flowing.clear()

    def resume(self) -> None:
        self._flowing.set()

    def cut_after(self, nbytes: int, *, rst: bool) -> None:
        """Drop every connection once `nbytes` more bytes went this way (exactly that many)."""
        self._budget = nbytes
        self._rst = rst

    async def flowing(self) -> None:
        """Return once not stalled."""
        await self._flowing.wait()

    def consume(self, size: int) -> tuple[int, bool | None]:
        """How many of `size` bytes to forward now, and whether to cut the connections after
        them: None for no cut, else whether with RST."""
        budget = self._budget
        if budget is None or size < budget:
            if budget is not None:
                self._budget = budget - size
            self.forwarded += size
            return size, None
        self._budget = None
        self.forwarded += budget
        return budget, self._rst


@dataclass(slots=True, eq=False)
class _Link:
    """One proxied connection."""

    client: asyncio.StreamWriter
    server: asyncio.StreamWriter

    def drop(self, *, rst: bool) -> None:
        _drop(self.client, rst=rst)
        _drop(self.server, rst=rst)


@dataclass(slots=True, eq=False)
class FaultProxy:
    """A TCP forwarder on 127.0.0.1 to `target`; `to_server` and `to_client` set the faults.

    Each direction of each connection is read by one task and written by another, through a
    queue of ``(due time, bytes)``: a delay holds bytes without slowing the reads behind them.
    """

    target: tuple[str, int]
    to_server: Faults = field(default_factory=Faults)
    to_client: Faults = field(default_factory=Faults)
    refuse: bool = False
    """Reset new connections at once, as a server that refuses them would."""
    accepted: int = 0
    """Connections accepted so far."""
    port: int = 0
    """The port it listens on, once started."""
    _links: list[_Link] = field(default_factory=list[_Link])
    _tasks: set[asyncio.Task[None]] = field(default_factory=set[asyncio.Task[None]])
    _server: asyncio.Server | None = None

    async def _read(
        self,
        source: asyncio.StreamReader,
        queue: asyncio.Queue[tuple[float, bytes]],
        faults: Faults,
    ) -> None:
        loop = asyncio.get_running_loop()
        with contextlib.suppress(OSError):
            while data := await source.read(65536):
                queue.put_nowait((loop.time() + faults.delay, data))
        faults.ended += 1
        queue.put_nowait((loop.time() + faults.delay, b""))  # EOF

    async def _send(self, sink: asyncio.StreamWriter, data: bytes, faults: Faults) -> bool:
        """Write `data` to `sink` as `faults` say; False once the connections were cut."""
        step = faults.fragment or len(data)
        for start in range(0, len(data), step):
            size, cut = faults.consume(min(step, len(data) - start))
            sink.write(data[start : start + size])
            await sink.drain()
            if cut is not None:
                if cut:
                    await self._drained(sink)
                self.drop(rst=cut)
                return False
        return True

    async def _drained(self, sink: asyncio.StreamWriter) -> None:
        """Wait until `sink`'s receiver has (most likely) read everything sent to it.

        macOS drops an RST that arrives while the receiver's window is closed (its buffer
        full), which leaves the receiver's socket open forever, as a lost RST on a real
        network would: no client can notice that without TCP keepalives or a timeout.
        """
        while sink.transport.get_write_buffer_size():
            await asyncio.sleep(0.001)
        await asyncio.sleep(0.1)

    async def _write(
        self,
        sink: asyncio.StreamWriter,
        queue: asyncio.Queue[tuple[float, bytes]],
        faults: Faults,
    ) -> None:
        loop = asyncio.get_running_loop()
        with contextlib.suppress(OSError):
            while True:
                due, data = await queue.get()
                await faults.flowing()
                await asyncio.sleep(max(0.0, due - loop.time()))
                await faults.flowing()  # stalled meanwhile
                if not data or not await self._send(sink, data, faults):
                    break
        sink.close()

    async def _flow(
        self,
        source: asyncio.StreamReader,
        sink: asyncio.StreamWriter,
        faults: Faults,
    ) -> None:
        queue: asyncio.Queue[tuple[float, bytes]] = asyncio.Queue()
        await asyncio.gather(self._read(source, queue, faults), self._write(sink, queue, faults))

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.accepted += 1
        if self.refuse:
            _drop(writer, rst=True)
            return
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(*self.target)
        except OSError:
            writer.transport.abort()
            return
        for side in (writer, upstream_writer):
            sock = side.get_extra_info("socket")
            if sock.family != socket.AF_UNIX:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._links.append(_Link(writer, upstream_writer))
        await asyncio.gather(
            self._flow(reader, upstream_writer, self.to_server),
            self._flow(upstream_reader, writer, self.to_client),
        )

    def _track(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.get_running_loop().create_task(self._handle(reader, writer))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def start(self) -> int:
        """Listen on 127.0.0.1; return the port."""
        self._server = await asyncio.start_server(self._track, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def start_unix(self, path: str) -> None:
        """Listen on the unix socket `path` instead."""
        self._server = await asyncio.start_unix_server(self._track, path)

    def route(self, dsn: str) -> str:
        """URL `dsn` pointed at this (started) proxy."""
        return at_address(dsn, "127.0.0.1", self.port)

    def stall(self) -> None:
        """Stop forwarding both ways, as a server or network that stopped responding."""
        self.to_server.stall()
        self.to_client.stall()

    def resume(self) -> None:
        self.to_server.resume()
        self.to_client.resume()

    def drop(self, *, rst: bool) -> None:
        """Close every open connection at once, both sides, with RST or with FIN."""
        links, self._links = self._links, []
        for link in links:
            link.drop(rst=rst)

    async def close(self) -> None:
        self.resume()
        self.drop(rst=True)
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)


async def _docker(*args: str, check: bool = True) -> None:
    process = await asyncio.create_subprocess_exec(
        "docker",
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    if check and process.returncode:
        msg = f"docker {' '.join(args)} failed: {stderr.decode()}"
        raise RuntimeError(msg)


@dataclass(slots=True, eq=False)
class DisposableServer:
    """A PostgreSQL container of the tests' own (never the shared one), on a fixed host port,
    so that a restart keeps its address."""

    name: str
    dsn: str

    async def ready(self, timeout: float = 30) -> float:
        """Wait until a new connection answers a query; return the seconds it took."""
        start = time.monotonic()
        async with asyncio.timeout(timeout):
            while True:
                try:
                    async with asyncio.timeout(2):
                        await _answers(self.dsn)
                except fastpg.Error, TimeoutError:
                    await asyncio.sleep(0.05)
                else:
                    return time.monotonic() - start

    async def pause(self) -> None:
        await _docker("pause", self.name)

    async def unpause(self) -> None:
        await _docker("unpause", self.name)

    async def kill(self) -> None:
        await _docker("kill", self.name)

    async def start(self) -> None:
        await _docker("start", self.name)

    async def restart(self) -> None:
        await _docker("restart", "--time", "1", self.name)

    async def heal(self) -> None:
        """Bring the server back to running and answering, whatever a test did to it."""
        await _docker("unpause", self.name, check=False)  # fails unless paused
        await _docker("start", self.name)  # a no-op when running
        await self.ready()


async def _answers(dsn: str) -> None:
    async with await fastpg.connect(dsn) as conn:
        await conn.select_one("SELECT 1")


@contextlib.contextmanager
def disposable_server(password: str, name: str) -> Generator[DisposableServer]:
    """Start a throwaway server on a fixed host port (so it survives restarts); remove it after.

    Its readiness is left to the caller (`DisposableServer.ready`).
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    container = (
        DockerContainer("postgres:18.6-trixie")
        .with_name(name)
        .with_env("POSTGRES_USER", "fastpg")
        .with_env("POSTGRES_PASSWORD", password)
        .with_env("POSTGRES_DB", "fastpg")
        .with_bind_ports(5432, port)
    )
    with container:
        yield DisposableServer(name, f"postgresql://fastpg:{password}@127.0.0.1:{port}/fastpg")
