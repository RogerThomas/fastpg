#!yeet
"""Loop-thread CPU per decoded cell, by PostgreSQL type, with the cyclic GC on and off.

Each case selects one ``people`` column eight times over a range of rows, as fastpg tuples,
and is compared with a zero-column ``SELECT`` over the same rows:

    ns per cell = (ns per row of the case - ns per row with no columns) / 8

This covers everything a cell costs on the event-loop thread: protocol parsing, the copy into
the row arena, decoding and the Python object, and freeing it again. Running every case with
the cyclic garbage collector on and off shows how much of that is collections triggered by
the objects a type allocates (``gc on - gc off``).

Three more cases build the full ten-column ``Person`` row as a tuple, a msgspec Struct and a
pydantic model; their differences are the per-row cost of each container.

Cases run round-robin, ``--repeats`` times, and each figure is the median over the repeats.
Measured on the loop thread with ``time.thread_time_ns``, like ``bench/decode_cost.py``, but
over each sample's whole loop of fetches, so the collections the fetches trigger and the
freeing of their results are always counted (see ``_sample``).

    uv run yeet ./bench/type_cost.py [-r 10000] [-n 10] [--repeats 7]
"""

import gc
import statistics
import time
from dataclasses import dataclass
from typing import Annotated

from drivers.base import DEFAULT_DSN, TABLE_ROWS, Person, PersonModel
from harness import run_coroutine
from rich.console import Console
from rich.table import Table
from yeetr import Opt

import fastpg

PERSON_COLUMNS = "id, name, age, email, score, balance, active, external_id, created_at, birthday"


@dataclass(frozen=True, slots=True)
class Case:
    name: str
    columns: str
    into: type[object] | None = None
    cells: int = 8


def _repeat(expr: str) -> str:
    return ", ".join(f"{expr} AS c{i}" for i in range(8))


CASES = (
    Case("no columns (baseline)", "", cells=0),
    Case("bool", _repeat("active")),
    Case("int4 (small, cached)", _repeat("age")),
    Case("int4", _repeat("id")),
    Case("int8", _repeat("id::int8")),
    Case("float8", _repeat("score")),
    Case("text (~11 chars)", _repeat("name")),
    Case("numeric(12,2)", _repeat("balance")),
    Case("uuid", _repeat("external_id")),
    Case("date", _repeat("coalesce(birthday, date '2000-01-01')")),
    Case("timestamptz", _repeat("created_at")),
    Case("NULL", _repeat("NULL::int4")),
    Case("Person row, tuple", PERSON_COLUMNS, cells=0),
    Case("Person row, msgspec Struct", PERSON_COLUMNS, into=Person, cells=0),
    Case("Person row, pydantic model", PERSON_COLUMNS, into=PersonModel, cells=0),
)


async def _fetch(conn: fastpg.Connection, case: Case, start: int, rows: int) -> int:
    sql = f"SELECT {case.columns} FROM people WHERE id BETWEEN $1 AND $2"  # noqa: S608 - fixed
    end = start + rows - 1
    if case.into is None:
        return len(await conn.select_many(sql, start, end))
    return len(await conn.select_many(sql, start, end, into=case.into))


async def _sample(conn: fastpg.Connection, case: Case, rows: int, fetches: int) -> float:
    """Loop-thread CPU ns per row over `fetches` fetches.

    The clock spans the whole loop on purpose. A collection that decoding schedules runs at
    the interpreter's next periodic check, which can fall after the awaited fetch has
    returned and the clock was read, and a result is freed only once the event loop drops
    its future. Whether a clock around each fetch sees either depends on the code around
    the `await`.
    """
    gc.collect()
    c0 = time.thread_time_ns()
    for i in range(fetches):
        start = 1 + (i * rows) % (TABLE_ROWS - rows)
        got = await _fetch(conn, case, start, rows)
        assert got == rows  # noqa: S101 - sanity check
    return (time.thread_time_ns() - c0) / (fetches * rows)


async def _measure(
    dsn: str,
    rows: int,
    fetches: int,
    repeats: int,
) -> dict[tuple[str, bool], float]:
    """Median ns per row for every (case, gc enabled)."""
    conn = await fastpg.connect(dsn)
    samples: dict[tuple[str, bool], list[float]] = {}
    try:
        for case in CASES:  # warm the statement cache and the struct bindings
            await _fetch(conn, case, 1, rows)
        for _ in range(repeats):
            for case in CASES:
                for gc_on in (True, False):
                    if not gc_on:
                        gc.disable()
                    try:
                        ns = await _sample(conn, case, rows, fetches)
                    finally:
                        gc.enable()
                    samples.setdefault((case.name, gc_on), []).append(ns)
    finally:
        await conn.close()
    return {key: statistics.median(values) for key, values in samples.items()}


def _table(medians: dict[tuple[str, bool], float], rows: int) -> Table:
    table = Table(title=f"Loop-thread CPU per cell / per row ({rows:,}-row fetches, medians)")
    table.add_column("case", no_wrap=True)
    for col in ("ns/row, gc on", "ns/row, gc off", "ns/cell, gc on", "ns/cell, gc off"):
        table.add_column(col, justify="right")
    base = {gc_on: medians[CASES[0].name, gc_on] for gc_on in (True, False)}
    for case in CASES:
        on, off = medians[case.name, True], medians[case.name, False]
        per_cell = ["", ""]
        if case.cells:
            per_cell = [
                f"{(on - base[True]) / case.cells:,.1f}",
                f"{(off - base[False]) / case.cells:,.1f}",
            ]
        table.add_row(case.name, f"{on:,.0f}", f"{off:,.0f}", *per_cell)
    return table


def main(
    *,
    rows: Annotated[int, Opt("-r", help="Rows per fetch")] = 10_000,
    fetches: Annotated[int, Opt("-n", help="Fetches per sample")] = 10,
    repeats: Annotated[int, Opt(help="Samples per case (round-robin)")] = 7,
    dsn: Annotated[str, Opt(envvar="FASTPG_DSN", help="Database URL")] = DEFAULT_DSN,
) -> None:
    """Measure loop-thread CPU per decoded cell for each column type.

    Args:
        rows: Rows per fetch.
        fetches: Fetches per sample.
        repeats: Samples per case; cases run round-robin and the median is reported.
        dsn: PostgreSQL DSN.
    """
    medians = run_coroutine(_measure(dsn, rows, fetches, repeats))
    Console().print(_table(medians, rows))
