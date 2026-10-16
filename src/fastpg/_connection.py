"""Connections and transactions.

A connection is bound to the event loop that opened it, and that loop's thread runs its
PostgreSQL protocol; fastpg starts no threads of its own. ``connect()`` connects the socket
in Python (host names are resolved with ``loop.getaddrinfo``; numeric addresses need no
resolver thread). The Rust core (``fastpg._native``) then drives it from ``loop.add_reader``
/ ``add_writer`` callbacks, polling xitca-postgres's handshake, driver and queries as tasks
on the loop thread.
"""

import asyncio
import os
import socket
import warnings
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from string.templatelib import Template
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Self, overload

from fastpg import _writes
from fastpg._named import NamedBinder, NamedParams
from fastpg._native import Connection as NativeConnection
from fastpg._native import dsn_targets
from fastpg._spec import Planner
from fastpg._template import TemplateCompiler
from fastpg.errors import (
    DatabaseError,
    DecodeError,
    FastPGError,
    InterfaceError,
    UnsupportedTypeError,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable, Iterable, Sequence
    from types import TracebackType

type Strategy = Literal["auto", "direct", "msgpack", "json"]
"""How rows become ``into=`` objects (``msgspec.Struct`` or pydantic ``BaseModel``).

* ``"direct"``: Rust decodes each column and builds the object itself: Structs via their
  ``__init__`` (vectorcall, no intermediate containers), pydantic models like
  ``model_construct`` (``__dict__`` + slots, no validation). Field types are checked against
  column types once per statement. Not available when the type needs validation
  (constraints, validators, enums, literals, ...).
* ``"msgpack"`` (msgspec only): Rust encodes rows as MessagePack; a cached
  ``msgspec.msgpack.Decoder`` builds and fully validates them.
* ``"json"``: Rust encodes rows as JSON; ``msgspec.json.Decoder`` or pydantic-core's
  ``validate_json`` builds and fully validates them.
* ``"auto"`` (default): ``"direct"`` when possible, otherwise ``"msgpack"`` (msgspec) or
  ``"json"`` (pydantic).

``into=dict`` and ``into=`` a ``NamedTuple`` are always built directly; ``"msgpack"`` and
``"json"`` raise ``UnsupportedTypeError`` for them.
"""

type IsolationLevel = Literal[
    "read_uncommitted",
    "read_committed",
    "repeatable_read",
    "serializable",
]
"""Transaction isolation level for ``transaction(isolation=...)``.

``None`` (the default there) keeps the server's ``default_transaction_isolation``.
"""

_ONE = 0
_ONE_OR_NONE = 1
_ALL = 2


@dataclass(slots=True, eq=False)
class _Queries:
    """The query methods of `Connection` and `Transaction`, run on one native connection."""

    _native: NativeConnection
    _loop: asyncio.AbstractEventLoop
    _templates: ClassVar[TemplateCompiler] = TemplateCompiler(max_size=1024)
    _named: ClassVar[NamedBinder] = NamedBinder(1024)

    @staticmethod
    async def _decoded(
        future: asyncio.Future[bytes | None],
        decode: Callable[[bytes], object],
        error: type[Exception],
    ) -> object:
        """The result of a msgpack/json fetch, decoded (and validated) by `decode`."""
        data = await future
        if data is None:
            return None
        try:
            return decode(data)
        except error as e:
            raise DecodeError(str(e)) from e

    def _fetch(
        self,
        query: str | Template,
        params: tuple[object, ...],
        shape: int,
        into: type[Any] | None,
        strategy: Strategy,
        future: asyncio.Future[Any] | None = None,
    ) -> Awaitable[Any]:
        """Submit the query and return what resolves to its result.

        Not a coroutine: the tuple and direct routes return the native future itself, so a
        query method awaits it without a coroutine in between. The native future is
        ``future`` if given (the msgpack and json routes return a coroutine awaiting it).
        """
        if isinstance(query, Template):
            query, params = self._templates.bind(query, params)
        if future is None:
            future = self._loop.create_future()
        if into is None:
            self._native.fetch(future, query, params, shape)
            return future
        plan = Planner.plan_for(into)
        if strategy == "auto":
            strategy = "direct" if plan.spec is not None else plan.fallback
        if strategy == "direct":
            if plan.spec is None:
                msg = f"strategy='direct' unavailable: {plan.direct_error}"
                raise UnsupportedTypeError(msg)
            self._native.fetch(future, query, params, shape, plan.spec)
            return future
        if strategy == "msgpack":
            one, many, error = plan.msgpack_one, plan.msgpack_many, plan.msgpack_error
            self_fetch = self._native.fetch_msgpack
        else:
            one, many, error = plan.json_one, plan.json_many, plan.json_error
            self_fetch = self._native.fetch_json
        if one is None or many is None or error is not None:
            detail = f"; direct construction unavailable: {plan.direct_error}"
            msg = f"strategy={strategy!r} unavailable: {error}{detail if plan.direct_error else ''}"
            raise UnsupportedTypeError(msg)
        self_fetch(future, query, params, shape, plan.text_columns)
        return self._decoded(future, many if shape == _ALL else one, plan.validation_error)

    @overload
    async def select_one(
        self,
        query: str | Template,
        /,
        *args: object,
        params: NamedParams | None = None,
        into: type[dict[Any, Any]],
        strategy: Strategy = "auto",
    ) -> dict[str, Any]: ...
    @overload
    async def select_one[T](
        self,
        query: str | Template,
        /,
        *args: object,
        params: NamedParams | None = None,
        into: type[T],
        strategy: Strategy = "auto",
    ) -> T: ...
    @overload
    async def select_one(
        self,
        query: str | Template,
        /,
        *args: object,
        params: NamedParams | None = None,
    ) -> tuple[Any, ...]: ...
    async def select_one(
        self,
        query: str | Template,
        /,
        *args: object,
        params: NamedParams | None = None,
        into: type[Any] | None = None,
        strategy: Strategy = "auto",
    ) -> object:
        """Return exactly one row.

        Raises NoRowsError for an empty result and TooManyRowsError for more than one row.
        """
        if params is not None:
            query, args = self._named.bind(query, args, params)
        return await self._fetch(query, args, _ONE, into, strategy)

    @overload
    async def select_one_or_none(
        self,
        query: str | Template,
        /,
        *args: object,
        params: NamedParams | None = None,
        into: type[dict[Any, Any]],
        strategy: Strategy = "auto",
    ) -> dict[str, Any] | None: ...
    @overload
    async def select_one_or_none[T](
        self,
        query: str | Template,
        /,
        *args: object,
        params: NamedParams | None = None,
        into: type[T],
        strategy: Strategy = "auto",
    ) -> T | None: ...
    @overload
    async def select_one_or_none(
        self,
        query: str | Template,
        /,
        *args: object,
        params: NamedParams | None = None,
    ) -> tuple[Any, ...] | None: ...
    async def select_one_or_none(
        self,
        query: str | Template,
        /,
        *args: object,
        params: NamedParams | None = None,
        into: type[Any] | None = None,
        strategy: Strategy = "auto",
    ) -> object:
        """Return the only row, or None for an empty result (TooManyRowsError for >1 row)."""
        if params is not None:
            query, args = self._named.bind(query, args, params)
        return await self._fetch(query, args, _ONE_OR_NONE, into, strategy)

    @overload
    async def select_many(
        self,
        query: str | Template,
        /,
        *args: object,
        params: NamedParams | None = None,
        into: type[dict[Any, Any]],
        strategy: Strategy = "auto",
    ) -> list[dict[str, Any]]: ...
    @overload
    async def select_many[T](
        self,
        query: str | Template,
        /,
        *args: object,
        params: NamedParams | None = None,
        into: type[T],
        strategy: Strategy = "auto",
    ) -> list[T]: ...
    @overload
    async def select_many(
        self,
        query: str | Template,
        /,
        *args: object,
        params: NamedParams | None = None,
    ) -> list[tuple[Any, ...]]: ...
    async def select_many(
        self,
        query: str | Template,
        /,
        *args: object,
        params: NamedParams | None = None,
        into: type[Any] | None = None,
        strategy: Strategy = "auto",
    ) -> object:
        """Return every row."""
        if params is not None:
            query, args = self._named.bind(query, args, params)
        return await self._fetch(query, args, _ALL, into, strategy)

    async def execute(
        self,
        query: str | Template,
        /,
        *args: object,
        params: NamedParams | None = None,
    ) -> int:
        """Run a statement (prepared, with parameters) and return the affected row count."""
        if params is not None:
            query, args = self._named.bind(query, args, params)
        elif isinstance(query, Template):
            query, args = self._templates.bind(query, args)
        future: asyncio.Future[int] = self._loop.create_future()
        self._native.execute(future, query, args)
        return await future

    async def execute_script(self, query: str | Template, /) -> None:
        """Run one or more `;`-separated statements without parameters (simple protocol).

        A t-string may splice in ``Identifier``s and fragments, but has no value parameters.
        """
        if isinstance(query, Template):
            query = self._templates.script(query)
        future: asyncio.Future[None] = self._loop.create_future()
        self._native.execute_script(future, query)
        await future

    async def execute_many(self, query: str, rows: Iterable[Sequence[object]], /) -> int:
        """Run `query` once per row of positional parameters; return the total row count.

        One prepared statement; rows are encoded in Rust and pipelined (no round trip per
        row). All-or-nothing: on an error (``exc.row_index`` names the failing row) or
        cancellation nothing is written, and an open transaction is left as it was.
        Concurrent queries on this connection wait until the call completes.
        """
        return await _writes.execute_many(self._native, self._loop, query, rows)

    async def insert_many(
        self,
        table: _writes.Table,
        rows: Iterable[object],
        /,
        *,
        columns: Sequence[str] | None = None,
    ) -> int:
        """``INSERT`` rows into `table` (pipelined, like `execute_many`); return the count.

        Rows are msgspec Structs or pydantic models (columns: their encoded field names /
        aliases), mappings (columns: the first row's keys) or sequences (columns: the
        table's, in order). `columns` selects/orders the columns explicitly. `table` is
        ``"name"``, ``"schema.name"`` or a tuple of parts; names are quoted (exact match).
        """
        return await _writes.insert_many(self._native, self._loop, table, rows, columns)

    async def copy_in(
        self,
        table: _writes.Table,
        rows: Iterable[object],
        /,
        *,
        columns: Sequence[str] | None = None,
    ) -> int:
        """Load rows with binary ``COPY ... FROM STDIN``; return the number of rows copied.

        The fastest way to load many rows. Same row kinds and `columns` rules as
        `insert_many`; values are encoded in Rust for the table's column types and streamed
        in chunks. All-or-nothing, like `execute_many`.
        """
        return await _writes.copy_in(self._native, self._loop, table, rows, columns)


@dataclass(slots=True, eq=False)
class Connection(_Queries):
    """A single PostgreSQL connection bound to the event loop it was opened on.

    Run queries on it directly: ``await conn.select_one(...)``. Statements are prepared on
    first use and cached per connection (keyed by SQL text); results use PostgreSQL's binary
    format. A query is SQL with ``$1, $2, ...`` placeholders plus positional parameters, SQL
    with ``:name`` placeholders plus ``params=`` (a Struct, dataclass, pydantic model or
    mapping), or a t-string whose interpolations become the parameters
    (``t"... WHERE id = {pid}"``); see ``fastpg.Identifier`` for interpolating names.

    Concurrent queries from multiple tasks are pipelined over the one connection.
    Use as an async context manager, or call `close()`.
    """

    _closed: asyncio.Future[None]
    _released: bool = field(init=False, default=False)
    _transaction: Transaction | None = field(init=False, default=None)

    def _release(self) -> None:
        if not self._released:
            self._released = True
            if not self._loop.is_closed():
                self._loop.remove_reader(self._native.fileno())

    async def _close(self) -> None:
        self._native.close()
        await self._closed
        self._release()

    @property
    def closed(self) -> bool:
        return self._native.closed

    @property
    def cached_statements(self) -> int:
        """Number of server-side prepared statements currently cached for this connection."""
        return self._native.statement_cache_len()

    def transaction(
        self,
        *,
        isolation: IsolationLevel | None = None,
        readonly: bool = False,
        deferrable: bool = False,
    ) -> Transaction:
        """Start a transaction: ``async with conn.transaction() as tx: await tx.execute(...)``.

        Commits when the block exits normally and rolls back when it raises. Only one
        transaction can be active per connection (nest with ``tx.savepoint()``).

        The connection is still shared while the block runs: queries other tasks send
        through this Connection meanwhile are pipelined into the same session, so they run
        inside the transaction. Keep a connection with an open transaction to one task, e.g.
        via ``Pool.transaction()`` / ``Pool.acquire()``.
        """
        if self._transaction is not None and self._transaction.active:
            msg = "a transaction is already active on this connection; use tx.savepoint()"
            raise InterfaceError(msg)
        self._transaction = Transaction(
            self,
            isolation=isolation,
            readonly=readonly,
            deferrable=deferrable,
        )
        return self._transaction

    async def close(self) -> None:
        """Close gracefully: in-flight queries complete, then the session is terminated."""
        await self._close()

    def __del__(self) -> None:
        if not self._released:
            warnings.warn(f"unclosed fastpg connection {self!r}", ResourceWarning, stacklevel=1)
            self._native.close()
            self._release()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._close()


class Transaction(_Queries):
    """A transaction on one connection, with the connection's query methods run inside it.

    Use as ``async with conn.transaction() as tx:`` (or ``Pool.transaction()``): BEGIN on
    entry, COMMIT when the block exits normally, ROLLBACK when it raises (cancellation
    included). ``async with tx.savepoint():`` nests a savepoint that is rolled back, and the
    exception re-raised, if its block raises.

    COMMIT is preceded, in the same round trip, by a statement that fails when the
    transaction is already aborted (an error was caught inside the block and the block went
    on). PostgreSQL would turn that COMMIT into a silent ROLLBACK; here the block raises the
    server's ``DatabaseError`` (SQLSTATE 25P02) instead, after rolling back.

    Its query methods raise ``InterfaceError`` outside the block (before BEGIN, after COMMIT
    or ROLLBACK): they would run outside the transaction, and once ``Pool.transaction()``
    has released the connection, maybe inside another caller's.
    """

    __slots__ = ("_begin", "_depth", "_pre_commit", "_session", "_state")

    _NEW: ClassVar[int] = 0
    _ACTIVE: ClassVar[int] = 1
    _DONE: ClassVar[int] = 2

    _ISOLATION: ClassVar[dict[str, str]] = {
        "read_uncommitted": "READ UNCOMMITTED",
        "read_committed": "READ COMMITTED",
        "repeatable_read": "REPEATABLE READ",
        "serializable": "SERIALIZABLE",
    }

    def __init__(
        self,
        conn: Connection,
        *,
        isolation: IsolationLevel | None = None,
        readonly: bool = False,
        deferrable: bool = False,
        pre_commit: str = "SELECT 1",
    ) -> None:
        """Prefer ``Connection.transaction()``; ``pre_commit`` runs right before COMMIT.

        ``pre_commit`` must fail inside an aborted transaction (any statement does). The pool
        passes its session-reset statements here, saving a round trip.
        """
        # The query methods use a view of the session that refuses them unless the
        # transaction is active; BEGIN, COMMIT, ROLLBACK and savepoints use the session.
        super().__init__(conn._native.transaction_view(), conn._loop)
        self._session = conn._native
        begin = ["BEGIN"]
        if isolation is not None:
            try:
                begin.append(f"ISOLATION LEVEL {self._ISOLATION[isolation]}")
            except KeyError:
                msg = f"invalid isolation level {isolation!r}"
                raise ValueError(msg) from None
        if readonly:
            begin.append("READ ONLY")
        if deferrable:
            begin.append("DEFERRABLE")
        self._begin = " ".join(begin)
        self._pre_commit = pre_commit
        self._depth = 0
        self._state = self._NEW

    async def _script(self, query: str) -> None:
        future: asyncio.Future[None] = self._loop.create_future()
        self._session.execute_script(future, query)
        await future

    async def _rollback(self, query: str, exc: BaseException) -> None:
        """Run a rollback while ``exc`` propagates; a failure is noted on ``exc``, not raised."""
        try:
            await self._script(query)
        except FastPGError as e:
            exc.add_note(f"fastpg: {query!r} failed too: {e}")

    async def _commit(self) -> None:
        try:
            await self._script(f"{self._pre_commit}; COMMIT")
        except DatabaseError as e:
            if e.sqlstate == "25P02":  # in_failed_sql_transaction: COMMIT never ran
                e.add_note("fastpg: the transaction was aborted by an earlier error; rolled back")
                await self._rollback("ROLLBACK", e)
            raise

    @property
    def active(self) -> bool:
        """True between BEGIN and COMMIT/ROLLBACK."""
        return self._state == self._ACTIVE

    @asynccontextmanager
    async def savepoint(self) -> AsyncGenerator[Self]:
        """Nest a savepoint: released on normal exit, rolled back to if the block raises."""
        if self._state != self._ACTIVE:
            msg = "savepoint() needs an active transaction"
            raise InterfaceError(msg)
        depth = self._depth + 1
        name = f"fastpg_savepoint_{depth}"
        await self._script(f"SAVEPOINT {name}")
        self._depth = depth
        try:
            yield self
        except BaseException as e:
            await self._rollback(f"ROLLBACK TO SAVEPOINT {name}; RELEASE SAVEPOINT {name}", e)
            raise
        else:
            await self._script(f"RELEASE SAVEPOINT {name}")
        finally:
            self._depth = depth - 1

    async def __aenter__(self) -> Self:
        if self._state != self._NEW:
            msg = "a Transaction can only be entered once"
            raise InterfaceError(msg)
        self._state = self._ACTIVE
        try:
            await self._script(self._begin)
        except BaseException as e:
            self._state = self._DONE
            if isinstance(e, asyncio.CancelledError):  # BEGIN may still run server-side
                await self._rollback("ROLLBACK", e)
            raise
        self._native.set_active(True)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._state = self._DONE
        self._native.set_active(False)
        if exc is None:
            await self._commit()
        else:
            await self._rollback("ROLLBACK", exc)


@dataclass(slots=True, eq=False)
class Fetcher(_Queries):
    """A connection's statement paths, for the pool (not public API).

    The pool already holds a statement's parameters as a tuple. Going through
    ``conn.select_*(query, *args, into=..., strategy=...)`` would repack them (a
    ``*args`` and keyword call, ~80 ns) and add a coroutine layer, on every shared
    statement; each method here is one plain call. Each submits the statement when called
    (not when awaited), with the caller's ``future`` as its native result future: the pool
    submits a waiting statement on its caller's behalf, and the caller awaits ``future``.
    """

    @classmethod
    def of(cls, conn: Connection) -> Self:
        return cls(conn._native, conn._loop)

    def fetch(
        self,
        future: asyncio.Future[Any],
        query: str,
        params: tuple[object, ...],
        shape: int,
        into: type[Any] | None,
        strategy: Strategy,
    ) -> Awaitable[Any]:
        """``select_one`` (shape 0), ``select_one_or_none`` (1) or ``select_many`` (2).

        Returns ``future`` itself, or (msgpack and json) a coroutine that decodes its result.
        """
        return self._fetch(query, params, shape, into, strategy, future)

    def send_execute(
        self,
        future: asyncio.Future[Any],
        query: str,
        params: tuple[object, ...],
    ) -> None:
        """``execute``: ``future`` gets the affected row count."""
        self._native.execute(future, query, params)

    def send_script(self, future: asyncio.Future[Any], query: str) -> None:
        """``execute_script``: ``future`` gets None."""
        self._native.execute_script(future, query)


type _Address = tuple[Any, ...] | str
"""A resolved socket address: `(host, port, ...)` or a unix path."""


def _set_done(future: asyncio.Future[None]) -> None:
    if not future.done():
        future.set_result(None)


async def _sock_connect(
    loop: asyncio.AbstractEventLoop,
    sock: socket.socket,
    address: _Address,
) -> None:
    """Connect the non-blocking ``sock`` to an already resolved ``address``.

    What ``loop.sock_connect`` does, minus its resolving: uvloop's passes every TCP address
    through ``getaddrinfo`` in libuv's thread pool, which starts four threads.
    """
    try:
        sock.connect(address)
    except BlockingIOError:
        pass
    else:
        return
    connected: asyncio.Future[None] = loop.create_future()
    fd = sock.fileno()
    loop.add_writer(fd, _set_done, connected)
    try:
        await connected
    finally:
        loop.remove_writer(fd)
    error = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
    if error:
        msg = f"connect to {address!r} failed: {os.strerror(error)}"
        raise OSError(error, msg)


async def _connect_candidate(
    loop: asyncio.AbstractEventLoop,
    family: socket.AddressFamily,
    address: _Address,
) -> socket.socket:
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.settimeout(0.0)  # non-blocking (as `setblocking(False)`, without a bare boolean)
    try:
        await _sock_connect(loop, sock, address)
    except BaseException:
        sock.close()
        raise
    if family != socket.AF_UNIX:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return sock


async def _resolve(
    loop: asyncio.AbstractEventLoop,
    host: str,
    port: int,
) -> list[tuple[socket.AddressFamily, _Address]]:
    """The TCP addresses of ``host``, in connection order.

    A numeric address is converted in place (``AI_NUMERICHOST`` never does a lookup):
    ``loop.getaddrinfo`` always runs in the loop's default executor, i.e. it would start a
    thread for an address that needs no resolving. Names still go through the loop.
    """
    try:
        infos = socket.getaddrinfo(
            host,
            port,
            type=socket.SOCK_STREAM,
            flags=socket.AI_NUMERICHOST,
        )
    except socket.gaierror:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [(family, address) for family, _, _, _, address in infos]


async def _open_socket(
    loop: asyncio.AbstractEventLoop,
    dsn: str,
) -> tuple[socket.socket, str]:
    """Connect the database socket from the loop (numeric hosts need no thread).

    Returns the socket and the DSN host it reached (the TLS server name).
    """
    failures: list[str] = []
    for kind, host, port in dsn_targets(dsn):
        if kind == "unix":
            path = Path(host)
            if path.is_dir():  # libpq semantics: a directory means <dir>/.s.PGSQL.<port>
                path /= f".s.PGSQL.{port}"
            candidates: list[tuple[socket.AddressFamily, _Address]] = [
                (socket.AF_UNIX, str(path)),
            ]
        else:
            try:
                candidates = await _resolve(loop, host, port)
            except OSError as e:
                failures.append(str(e))
                continue
        for family, address in candidates:
            try:
                return await _connect_candidate(loop, family, address), host
            except OSError as e:
                failures.append(str(e))
    reason = "; ".join(failures) or "no host to connect to"
    msg = f"could not connect to server: {reason}"
    raise InterfaceError(msg)


def _detach_socket(loop: asyncio.AbstractEventLoop, sock: socket.socket) -> None:
    """Unregister and close the database socket; Rust calls this once its driver is gone."""
    if not loop.is_closed():
        loop.remove_reader(sock.fileno())
        loop.remove_writer(sock.fileno())
    sock.close()


async def connect(dsn: str, *, statement_cache_size: int = 256) -> Connection:
    """Open a connection, e.g. ``await connect("postgresql://user:pw@host:5432/db")``.

    TLS takes libpq's DSN keys: ``sslmode`` (``disable``, ``prefer`` (the default),
    ``require``, ``verify-ca``, ``verify-full``), ``sslrootcert`` (a CA file, or ``system``;
    ``verify-*`` without it uses the system's trusted roots), ``sslnegotiation=direct``,
    ``sslcert``/``sslkey``, ``sslsni`` and ``channel_binding`` (``prefer`` (the default),
    ``require``, ``disable``). Unix sockets never use TLS. See ``notes/tls.md``.

    The connection is bound to the running event loop, whose thread runs its protocol and
    waits on the database socket directly: no extra threads, and a query costs no thread
    hand-offs.
    """
    loop = asyncio.get_running_loop()
    native = NativeConnection(statement_cache_size)
    # Wakes the loop for the rare task woken from another thread (e.g. by a `__del__` there).
    fd = native.fileno()
    loop.add_reader(fd, native.drain)
    connected: asyncio.Future[None] = loop.create_future()
    closed: asyncio.Future[None] = loop.create_future()
    try:
        sock, host = await _open_socket(loop, dsn)
        sfd = sock.fileno()
        # Registered first: attach() can fail at once (e.g. a TLS file is missing), and then
        # detaches (unregisters and closes) the socket before returning.
        loop.add_reader(sfd, native.on_readable)
        native.attach(
            dsn,
            sfd,
            unix=sock.family == socket.AF_UNIX,
            host=host,
            add_writer=partial(loop.add_writer, sfd, native.on_writable),
            remove_writer=partial(loop.remove_writer, sfd),
            flush_soon=partial(loop.call_soon, native.flush),
            detach=partial(_detach_socket, loop, sock),
            connected=connected,
            closed=closed,
        )
        await connected
    except BaseException:
        native.close()  # once attached, the socket is detached and closed by native
        loop.remove_reader(fd)
        raise
    return Connection(native, loop, closed)
