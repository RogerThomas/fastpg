#!yeet
"""Client-side cost of ``:name`` queries with ``params=``, against positional parameters.

``main`` times ``Connection.select_one_or_none(..., into=Struct)`` calls with no database
involved: the native connection is a stub that resolves each query at once, so what's left
is the Python work a call does before reaching Rust (``timeit``, best of several repeats,
nanoseconds per call). Scenarios: ``$1, $2`` with positional parameters, and the same query
as ``:id, :min_age`` with ``params=`` a Struct, a dataclass and a dict.

``end_to_end`` compares event-loop-thread CPU (``time.thread_time()``) per sequential point
lookup into a Struct, through a one-connection pool against a live database: positional vs
``params=`` Struct vs dict. Its run-to-run noise is far above the difference measured here.

    uv run yeet ./bench/named_overhead.py [--number 200000]
    uv run yeet ./bench/named_overhead.py end_to_end [--queries 20000]
"""

import asyncio
import dataclasses
import time
import timeit
from typing import Annotated, Any

import msgspec
from drivers.base import DEFAULT_DSN
from harness import run_coroutine
from yeetr import Opt

import fastpg

POSITIONAL = "SELECT name, age FROM people WHERE id = $1 AND age >= $2"
NAMED = "SELECT name, age FROM people WHERE id = :id AND age >= :min_age"


class _Person(msgspec.Struct):
    name: str
    age: int


class _Select(msgspec.Struct):
    id: int
    min_age: int


@dataclasses.dataclass(slots=True)
class _DataclassSelect:
    id: int
    min_age: int


class _Native:
    """Stands in for the native connection: every query resolves at once, with no row."""

    def fetch(self, future: asyncio.Future[Any], *_args: object) -> None:
        future.set_result(None)

    def fileno(self) -> int:
        return 0  # never registered with the loop, so removing it is a no-op

    def close(self) -> None:
        pass


class _Runner:
    """Callables for timeit: each drives one query method call to completion."""

    def __init__(self, conn: fastpg.Connection) -> None:
        self._conn = conn
        self._struct = _Select(id=42, min_age=30)
        self._dataclass = _DataclassSelect(id=42, min_age=30)
        self._dict = {"id": 42, "min_age": 30}

    @staticmethod
    def _drive(coro: Any) -> None:  # noqa: ANN401 - any query method's coroutine
        """Run `coro`: awaiting the stub's already-resolved future never suspends it."""
        try:
            coro.send(None)
        except StopIteration:
            return
        msg = "the stubbed query suspended"
        raise RuntimeError(msg)

    def positional(self) -> None:
        self._drive(self._conn.select_one_or_none(POSITIONAL, 42, 30, into=_Person))

    def struct(self) -> None:
        self._drive(self._conn.select_one_or_none(NAMED, params=self._struct, into=_Person))

    def dataclass(self) -> None:
        self._drive(self._conn.select_one_or_none(NAMED, params=self._dataclass, into=_Person))

    def dict(self) -> None:
        self._drive(self._conn.select_one_or_none(NAMED, params=self._dict, into=_Person))


def _time(runner: _Runner, number: int, repeat: int) -> None:
    scenarios = {
        "positional ($1, $2)": runner.positional,
        "params= Struct": runner.struct,
        "params= dataclass": runner.dataclass,
        "params= dict": runner.dict,
    }
    for scenario in scenarios.values():
        scenario()  # warm the caches
    width = max(map(len, scenarios))
    base = 0.0
    for name, scenario in scenarios.items():
        best = min(timeit.repeat(scenario, number=number, repeat=repeat)) / number * 1e9
        base = base or best
        print(f"{name:<{width}}  {best:6.0f} ns  (+{best - base:4.0f} ns)")


async def _main(number: int, repeat: int) -> None:
    loop = asyncio.get_running_loop()
    native: Any = _Native()
    closed: asyncio.Future[None] = loop.create_future()
    conn = fastpg.Connection(native, loop, closed)
    _time(_Runner(conn), number, repeat)
    closed.set_result(None)
    await conn.close()


def main(*, number: int = 200_000, repeat: int = 7) -> None:
    asyncio.run(_main(number, repeat))


async def _lookups(pool: fastpg.Pool, queries: int, params: object | None) -> float:
    start = time.thread_time()
    if params is None:
        for _ in range(queries):
            await pool.select_one(POSITIONAL, 42, 0, into=_Person)
    else:
        named: Any = params
        for _ in range(queries):
            await pool.select_one(NAMED, params=named, into=_Person)
    return (time.thread_time() - start) / queries


async def _end_to_end(dsn: str, queries: int, rounds: int) -> None:
    variants: dict[str, object | None] = {
        "positional ($1, $2)": None,
        "params= Struct": _Select(id=42, min_age=0),
        "params= dict": {"id": 42, "min_age": 0},
    }
    async with await fastpg.create_pool(dsn, min_size=1, max_size=1) as pool:
        for params in variants.values():
            await _lookups(pool, 1000, params)
        best = {name: float("inf") for name in variants}
        for _ in range(rounds):  # interleaved, so drift hits every variant alike
            for name, params in variants.items():
                best[name] = min(best[name], await _lookups(pool, queries, params))
    for name, cpu in best.items():
        print(f"pool, loop-thread CPU per point lookup, {name:<20} {cpu * 1e6:5.2f} µs")


def end_to_end(
    *,
    queries: int = 20_000,
    rounds: int = 5,
    dsn: Annotated[str, Opt(envvar="FASTPG_DSN", help="Database URL")] = DEFAULT_DSN,
) -> None:
    run_coroutine(_end_to_end(dsn, queries, rounds))
