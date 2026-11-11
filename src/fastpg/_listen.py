"""LISTEN/NOTIFY: ``listen()`` on pools and connections, and the notifications it yields.

The connection's driver (``native/src/listen.rs``) queues each notification for the open
listeners of its channel and wakes the one waiting, the way a query's result does. This
module sends LISTEN and UNLISTEN, and turns the queue into an async iterator.
"""

import asyncio
from dataclasses import dataclass
from typing import Self

from fastpg._native import Listener as NativeListener
from fastpg._template import Identifier
from fastpg.errors import InterfaceError


@dataclass(frozen=True, slots=True)
class Notification:
    """A notification received by a listener (see ``Pool.listen()``)."""

    channel: str
    """The channel it was sent on."""
    payload: str
    """Its payload: ``""`` when none was given."""
    pid: int
    """The process id of the server backend that sent it."""


class Channels:
    """The checked channel names of a ``listen()`` call (not public API)."""

    _MAX_BYTES = 63
    """PostgreSQL's longest name (NAMEDATALEN - 1): LISTEN would truncate a longer one, and
    then never match the full name, which ``pg_notify`` refuses anyway."""

    __slots__ = ("names",)

    def __init__(self, channels: tuple[str, ...], max_queued: int) -> None:
        if not channels:
            msg = "listen() needs at least one channel"
            raise ValueError(msg)
        if type(max_queued) is not int or max_queued < 1:  # for untyped callers
            msg = f"max_queued must be an int >= 1, got {max_queued!r}"
            raise ValueError(msg)
        for channel in channels:
            Identifier(channel)  # a non-empty str without NUL
            if len(channel.encode()) > self._MAX_BYTES:
                msg = f"channel name {channel!r} is longer than {self._MAX_BYTES} bytes"
                raise ValueError(msg)
        self.names = list(dict.fromkeys(channels))

    @staticmethod
    def script(command: str, names: list[str]) -> str:
        """``LISTEN`` or ``UNLISTEN`` each of ``names``, quoted: matched exactly."""
        return "; ".join(f"{command} {Identifier(name).quoted}" for name in names)


class Listener:
    """The notifications on a ``listen()`` block's channels, as an async iterator.

    ``async for n in listener`` yields each `Notification` in the order the server sent
    them. Notifications sent before the block started are not seen. Up to ``max_queued``
    notifications wait to be read; if more arrive, the listener stops queueing and raises
    ``ListenerOverflowError`` (notifications were lost). Once the connection is lost or
    closed (the pool's too), it yields what was already queued, then raises
    ``ConnectionClosedError``. It works only inside its block.
    """

    _OTHER_LOOP = (
        "this listener belongs to a different event loop (the one that opened its connection):"
        " use it inside that loop"
    )

    __slots__ = ("_loop", "_native")

    def __init__(self, native: NativeListener, loop: asyncio.AbstractEventLoop) -> None:
        self._native = native
        self._loop = loop

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> Notification:
        if asyncio.get_running_loop() is not self._loop:
            raise InterfaceError(self._OTHER_LOOP)
        while True:
            item = self._native.pop()
            if item is not None:
                return Notification(*item)
            future: asyncio.Future[None] = self._loop.create_future()
            self._native.wait(future)
            await future
