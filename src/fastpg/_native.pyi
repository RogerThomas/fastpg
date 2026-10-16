"""Type stub for the Rust extension module (native/src)."""

import asyncio
from collections.abc import Callable, Sequence
from typing import Any, final

__version__: str

def rust_allocations() -> int | None:
    """Rust heap allocations so far, or None unless built with the `alloc-stats` feature."""

def after_fork_in_child() -> None:
    """Mark every existing connection as inherited (``os.register_at_fork`` hook)."""

def dsn_targets(dsn: str) -> list[tuple[str, str, int]]:
    """The hosts of `dsn` as `("tcp" | "unix", host or path, port)`, in connection order."""

@final
class StructSpec:
    def __new__(
        cls,
        struct_type: type[Any],
        fields: Sequence[tuple[str, str, int, bool, bool, object | None]],
        *,
        forbid_unknown_fields: bool,
        positional_fields: int,
        pydantic_defaults: Sequence[tuple[bool, object]] | None = None,
        named_tuple_defaults: Sequence[tuple[bool, object]] | None = None,
        named_tuple_alloc: bool = False,
        untrackable: bool = False,
    ) -> StructSpec: ...
    @staticmethod
    def for_dict() -> StructSpec:
        """The spec for `into=dict`: one dict per row, keyed by column name."""

@final
class Connection:
    def __new__(cls, statement_cache_size: int = 256) -> Connection: ...
    def transaction_view(self) -> Connection: ...
    def set_active(self, active: bool) -> None: ...
    @property
    def closed(self) -> bool: ...
    @property
    def inherited(self) -> bool: ...
    def fileno(self) -> int: ...
    def attach(
        self,
        dsn: str,
        fd: int,
        *,
        unix: bool,
        host: str,
        add_writer: Callable[[], object],
        remove_writer: Callable[[], object],
        flush_soon: Callable[[], object],
        detach: Callable[[], object],
        connected: asyncio.Future[None],
        closed: asyncio.Future[None],
    ) -> None: ...
    def on_readable(self) -> None: ...
    def on_writable(self) -> None: ...
    def flush(self) -> None: ...
    def close(self) -> None: ...
    def abandon(self) -> None: ...
    def fetch(
        self,
        future: asyncio.Future[Any],
        query: str,
        params: tuple[object, ...],
        shape: int,
        spec: StructSpec | None = None,
    ) -> None: ...
    def fetch_msgpack(
        self,
        future: asyncio.Future[Any],
        query: str,
        params: tuple[object, ...],
        shape: int,
        text_columns: tuple[str, ...] = (),
    ) -> None: ...
    def fetch_json(
        self,
        future: asyncio.Future[Any],
        query: str,
        params: tuple[object, ...],
        shape: int,
        text_columns: tuple[str, ...] = (),
    ) -> None: ...
    def execute(
        self,
        future: asyncio.Future[int],
        query: str,
        params: tuple[object, ...],
    ) -> None: ...
    def execute_script(self, future: asyncio.Future[None], query: str) -> None: ...
    def execute_many(
        self,
        ready: asyncio.Future[None],
        done: asyncio.Future[int | None],
        query: str,
        reader: RowReader | None = None,
    ) -> Bulk: ...
    def insert_many(
        self,
        ready: asyncio.Future[None],
        done: asyncio.Future[int | None],
        prefix: str,
        width: int,
        group: int,
        reader: RowReader,
    ) -> Bulk: ...
    def copy_in(
        self,
        ready: asyncio.Future[None],
        done: asyncio.Future[int | None],
        table: str,
        reader: RowReader,
        columns: Sequence[str] | None = None,
    ) -> Bulk: ...
    def drain(self) -> None: ...
    def statement_cache_len(self) -> int: ...

@final
class RowReader:
    """How to read column values from rows: 0 sequence, 1 mapping, 2 Struct, 3 pydantic."""

    def __new__(
        cls,
        kind: int,
        names: Sequence[str],
        *,
        labels: Sequence[str] = (),
        row_type: type[Any] | None = None,
        exact: bool = False,
    ) -> RowReader: ...

@final
class Bulk:
    """A running bulk write; see `fastpg._writes`."""

    def feed(self, ack: asyncio.Future[None], rows: list[Any]) -> None: ...
    def finish(self) -> None: ...
    def abort(self) -> None: ...

@final
class Router:
    """The pool's routing state for shared statements; see `fastpg._pool` and router.rs."""

    def __new__(cls, default_cost: int, max_costs: int) -> Router: ...
    def add(self) -> int: ...
    def remove(self, slot: int) -> None: ...
    def set_shared(self, slot: int, shared: bool) -> None: ...
    def touch(self, slot: int) -> None: ...
    def idle_ns(self, slot: int) -> int: ...
    def load(self, slot: int) -> int: ...
    def begin(self, slot: int, query: str) -> int: ...
    def finish(self, slot: int, token: int, query: str | None = None) -> int: ...
    def best(self, cap: int) -> int | None: ...
