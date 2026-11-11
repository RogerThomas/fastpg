"""LISTEN/NOTIFY: ``listen()`` on pools and connections, and the notifications it yields.

The connection's driver (``native/src/listen.rs``) queues each notification for the open
listeners of its channel and wakes the one waiting, the way a query's result does. This
module sends LISTEN and UNLISTEN, turns the queue into an async iterator, and decodes
payloads from JSON into ``into=``: the types queries take there, checked by the same
`Planner`. ``notify()`` sends what ``params=`` takes as JSON.
"""

import asyncio
import sys
import typing
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, LiteralString, Self, TypeIs

import msgspec
import msgspec.inspect as mi

from fastpg._named import NamedBinder, NamedParams
from fastpg._native import Listener as NativeListener
from fastpg._spec import Planner
from fastpg._template import Identifier
from fastpg.errors import DecodeError, InterfaceError

type Payload = str | NamedParams
"""What ``notify()`` sends: a ``str`` as it is, anything ``params=`` takes as JSON."""


@dataclass(frozen=True, slots=True)
class Notification[T = str]:
    """A notification received by a listener (see ``Pool.listen()``)."""

    channel: str
    """The channel it was sent on."""
    payload: T
    """Its payload: a ``str`` (``""`` when none was given), or with ``listen(into=...)``
    decoded from JSON into that type."""
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


class _Encoded[T]:
    """A plan's JSON decoder, which takes bytes, for payloads, which are ``str``s."""

    __slots__ = ("_decode",)

    def __init__(self, decode: Callable[[bytes], T]) -> None:
        self._decode = decode

    def __call__(self, payload: str) -> T:
        return self._decode(payload.encode())


class _NamedTupleFromJSON:
    """A JSON object into a NamedTuple, each field filled by its name as a column fills it
    (msgspec reads NamedTuples from JSON arrays only)."""

    __slots__ = ("_decode", "_target")

    def __init__(self, target: type[tuple[Any, ...]]) -> None:
        info = mi.type_info(target)
        if not isinstance(info, mi.NamedTupleType):  # guaranteed for namedtuple classes
            msg = f"msgspec does not describe {target!r} as a NamedTuple"
            raise TypeError(msg)
        hints = typing.get_type_hints(target)
        fields: list[tuple[str, Any] | tuple[str, Any, Any]] = [
            # A collections.namedtuple field has no annotation: Any.
            (f.name, hints.get(f.name, Any))
            if f.required
            else (f.name, hints.get(f.name, Any), f.default)
            for f in info.fields
        ]
        self._decode = msgspec.json.Decoder(msgspec.defstruct(target.__name__, fields)).decode
        self._target = target

    def __call__(self, payload: str) -> tuple[Any, ...]:
        return self._target(*msgspec.structs.astuple(self._decode(payload)))


class PayloadDecoder[T]:
    """How a listener turns payloads into ``into=``, built once per listener (not public
    API). ``errors``: what ``decode`` raises for a payload that doesn't fit."""

    __slots__ = ("decode", "errors")

    def __init__(
        self,
        decode: Callable[[str], T],
        errors: tuple[type[Exception], ...],
    ) -> None:
        self.decode = decode
        self.errors = errors

    @staticmethod
    def _is_tuple_type(into: object) -> TypeIs[type[tuple[Any, ...]]]:
        return isinstance(into, type) and issubclass(into, tuple)

    @staticmethod
    def build(into: object) -> "PayloadDecoder[Any]":
        """The decoder for ``into``: payloads as they are for ``str``; else, for a type
        queries take as ``into=`` (TypeError for any other, as queries raise), decoded from
        JSON by the query's decoder (a Struct, a pydantic model) or msgspec."""
        if into is str:
            return PayloadDecoder(str, ())
        plan = Planner.plan_for(into)
        if into is dict:
            decode_dict = msgspec.json.Decoder(dict[str, Any]).decode
            return PayloadDecoder(decode_dict, (msgspec.DecodeError,))
        if plan.json_one is not None:
            return PayloadDecoder(_Encoded(plan.json_one), (plan.validation_error,))
        if PayloadDecoder._is_tuple_type(into):  # the one kind left: NamedTuple
            return PayloadDecoder(_NamedTupleFromJSON(into), (msgspec.DecodeError,))
        msg = f"listen() can't decode into {into!r}"
        raise TypeError(msg)


class NotifyQuery:
    """The statement ``notify()`` runs for a payload, and the value it sends as ``$2`` (not
    public API)."""

    _TEXT: ClassVar[LiteralString] = "SELECT pg_notify($1, $2)"
    # A json parameter takes dicts, Structs and dataclasses, encoded as any JSON parameter
    # is (NaN and infinities refused); json keeps the text exactly as it was sent.
    _JSON: ClassVar[LiteralString] = "SELECT pg_notify($1, $2::json::text)"

    @classmethod
    def for_payload(cls, payload: Payload) -> tuple[LiteralString, object]:
        """TypeError for anything but a `Payload` (untyped callers)."""
        if isinstance(payload, str):
            return cls._TEXT, payload
        try:
            by_key = NamedBinder.read_by_key(type(payload))
        except TypeError as e:
            msg = f"payload must be a str, or what params= takes: {e}"
            raise TypeError(msg) from None
        if by_key and isinstance(payload, Mapping):
            return cls._JSON, payload if isinstance(payload, dict) else dict(payload)
        # Only consult pydantic if the application already imported it.
        if "pydantic" in sys.modules:
            from pydantic import BaseModel

            if isinstance(payload, BaseModel):
                # A dict for the json parameter (pydantic's own JSON writes NaN and the
                # infinities as null), keyed by alias, as listen(into=) validates it.
                return cls._JSON, payload.model_dump(mode="json", by_alias=True)
        return cls._JSON, payload


class Listener[T = str]:
    """The notifications on a ``listen()`` block's channels, as an async iterator.

    ``async for n in listener`` yields each `Notification` in the order the server sent
    them. Notifications sent before the block started are not seen. Up to ``max_queued``
    notifications wait to be read; if more arrive, the listener stops queueing and raises
    ``ListenerOverflowError`` (notifications were lost). Once the connection is lost or
    closed (the pool's too), it yields what was already queued, then raises
    ``ConnectionClosedError``. It works only inside its block.

    With ``listen(into=...)`` other than ``str``, a payload that doesn't decode raises
    ``DecodeError`` for that notification; the next one is read as usual.
    """

    _OTHER_LOOP = (
        "this listener belongs to a different event loop (the one that opened its connection):"
        " use it inside that loop"
    )

    __slots__ = ("_decoder", "_loop", "_native")

    def __init__(
        self,
        native: NativeListener,
        loop: asyncio.AbstractEventLoop,
        decoder: PayloadDecoder[T],
    ) -> None:
        self._native = native
        self._loop = loop
        self._decoder = decoder

    def _notification(self, channel: str, payload: str, pid: int) -> Notification[T]:
        try:
            value = self._decoder.decode(payload)
        except self._decoder.errors as e:
            msg = f"the payload of a notification on channel {channel!r} doesn't decode: {e}"
            raise DecodeError(msg) from e
        return Notification(channel, value, pid)

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> Notification[T]:
        if asyncio.get_running_loop() is not self._loop:
            raise InterfaceError(self._OTHER_LOOP)
        while True:
            item = self._native.pop()
            if item is not None:
                return self._notification(*item)
            future: asyncio.Future[None] = self._loop.create_future()
            self._native.wait(future)
            await future
