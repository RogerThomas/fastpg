#!yeet
"""Client-side cost of t-string queries: building the ``$n`` SQL and the parameter tuple.

Times what a ``Connection`` query does before handing a t-string query to the native layer,
with no database involved (``timeit``, best of several repeats, nanoseconds per call):

* ``str query``: the ``isinstance(query, Template)`` dispatch a plain-SQL call now pays;
* cached templates with 1 and 3 parameters (the common case: one t-string literal in a loop);
* the same SQL from a template rebuilt at runtime (cache hit on equality, not identity);
* a composed template (a nested fragment and an ``Identifier``);
* a cache miss (``Template`` objects with fresh static text every call).

Building the t-string itself is reported too: the interpreter pays that at the call site.

``end_to_end`` compares event-loop-thread CPU (``time.thread_time()``) per sequential point
lookup into a Struct, ``$1`` + param vs the same query as a t-string, against a live database.

    uv run yeet ./bench/template_overhead.py [--number 1000000]
    uv run yeet ./bench/template_overhead.py end_to_end [--queries 20000]
"""

import time
import timeit
from string.templatelib import Interpolation, Template
from typing import Annotated

import msgspec
from drivers.base import DEFAULT_DSN
from harness import run_coroutine
from yeetr import Opt

import fastpg
from fastpg import Identifier
from fastpg._template import TemplateCompiler


def _one(pid: int) -> Template:
    return t"SELECT name, age FROM people WHERE id = {pid}"


def _three(pid: int, age: int, name: str) -> Template:
    return t"SELECT name, age FROM people WHERE id = {pid} AND age >= {age} AND name = {name}"


def _composed(pid: int, age: int) -> Template:
    where = t"id = {pid} AND age >= {age}"
    return t"SELECT name, age FROM {Identifier('people')} WHERE {where} LIMIT {10}"


class _Runner:
    """Callables for timeit (no closures: one bound method per scenario)."""

    def __init__(self, compiler: TemplateCompiler) -> None:
        self._compiler = compiler
        self._counter = 0
        self._query: str | Template = "SELECT 1"
        self._one = _one(1)
        self._three = _three(1, 2, "name")
        self._rebuilt = Template(
            "SELECT name, age FROM people WHERE id = ",
            Interpolation(1, "pid"),
            " AND age >= ",
            Interpolation(2, "age"),
            " AND name = ",
            Interpolation("name", "name"),
            "",
        )
        self._composed = _composed(1, 2)

    def str_dispatch(self) -> object:
        query = self._query
        if isinstance(query, Template):
            return self._compiler.bind(query, ())
        return query

    def cached_one(self) -> object:
        return self._compiler.bind(self._one, ())

    def cached_three(self) -> object:
        return self._compiler.bind(self._three, ())

    def rebuilt_three(self) -> object:
        return self._compiler.bind(self._rebuilt, ())

    def composed(self) -> object:
        return self._compiler.bind(self._composed, ())

    def miss(self) -> object:
        self._counter += 1
        template = Template(f"SELECT {self._counter} WHERE id = ", Interpolation(1, "pid"), "")
        return self._compiler.bind(template, ())

    def build_three(self) -> object:
        return _three(1, 2, "name")


def main(*, number: int = 1_000_000, repeat: int = 7) -> None:
    runner = _Runner(TemplateCompiler(max_size=1024))
    scenarios = {
        "str query (dispatch only)": runner.str_dispatch,
        "cached, 1 parameter": runner.cached_one,
        "cached, 3 parameters": runner.cached_three,
        "rebuilt at runtime, 3 parameters (equality hit)": runner.rebuilt_three,
        "composed: fragment + Identifier, 3 parameters": runner.composed,
        "cache miss, 1 parameter": runner.miss,
        "(building the 3-parameter t-string itself)": runner.build_three,
    }
    for scenario in scenarios.values():
        scenario()  # warm the caches
    width = max(map(len, scenarios))
    for name, scenario in scenarios.items():
        n = number // 10 if scenario == runner.miss else number
        best = min(timeit.repeat(scenario, number=n, repeat=repeat)) / n
        print(f"{name:<{width}}  {best * 1e9:7.0f} ns")


class _Person(msgspec.Struct):
    name: str
    age: int


async def _lookups_str(conn: fastpg.Connection, queries: int) -> float:
    start = time.thread_time()
    for i in range(queries):
        await conn.select_one(
            "SELECT name, age FROM people WHERE id = $1",
            i % 1000 + 1,
            into=_Person,
        )
    return (time.thread_time() - start) / queries


async def _lookups_template(conn: fastpg.Connection, queries: int) -> float:
    start = time.thread_time()
    for i in range(queries):
        person_id = i % 1000 + 1
        await conn.select_one(t"SELECT name, age FROM people WHERE id = {person_id}", into=_Person)
    return (time.thread_time() - start) / queries


async def _end_to_end(dsn: str, queries: int, rounds: int) -> None:
    async with await fastpg.connect(dsn) as conn:
        await _lookups_str(conn, 1000)
        await _lookups_template(conn, 1000)
        plain = min([await _lookups_str(conn, queries) for _ in range(rounds)])
        template = min([await _lookups_template(conn, queries) for _ in range(rounds)])
    print(f"loop-thread CPU per point lookup, $1 + param:  {plain * 1e6:5.2f} µs")
    print(f"loop-thread CPU per point lookup, t-string:    {template * 1e6:5.2f} µs")


def end_to_end(
    *,
    queries: int = 20_000,
    rounds: int = 5,
    dsn: Annotated[str, Opt(envvar="FASTPG_DSN", help="Database URL")] = DEFAULT_DSN,
) -> None:
    run_coroutine(_end_to_end(dsn, queries, rounds))
