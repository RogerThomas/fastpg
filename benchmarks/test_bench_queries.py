"""Client-side cost of fastpg's query paths, measured with pytest-codspeed.

Each benchmark runs its query once before measuring, so the statement is already prepared
and cached and the `into=` type already planned: what's measured is the steady state.
"""

import datetime as dt
import decimal
import uuid
from typing import TYPE_CHECKING, Any, NamedTuple

import msgspec
import pytest
from pydantic import BaseModel

import fastpg

if TYPE_CHECKING:
    from collections.abc import Callable

    from conftest import Runner
    from pytest_codspeed import BenchmarkFixture

COLUMNS = "id, name, age, email, score, balance, active, external_id, created_at, birthday"
SELECT_ONE = f"SELECT {COLUMNS} FROM people WHERE id = $1"  # noqa: S608 - constant text
SELECT_RANGE = f"SELECT {COLUMNS} FROM people WHERE id >= $1 ORDER BY id LIMIT $2"  # noqa: S608


class Person(msgspec.Struct):
    id: int
    name: str
    age: int
    email: str | None
    score: float
    balance: decimal.Decimal
    active: bool
    external_id: uuid.UUID
    created_at: dt.datetime
    birthday: dt.date | None


class PersonModel(BaseModel):
    id: int
    name: str
    age: int
    email: str | None
    score: float
    balance: decimal.Decimal
    active: bool
    external_id: uuid.UUID
    created_at: dt.datetime
    birthday: dt.date | None


class ByAge(msgspec.Struct):
    id: int
    min_age: int


class Row(NamedTuple):
    id: int
    name: str
    score: float


class Tagged(msgspec.Struct):
    id: int
    tags: list[str]
    numbers: list[int]


class AgeGroup(msgspec.Struct):
    age: int
    ids: list[int]
    names: list[str]


def _measure[T](benchmark: BenchmarkFixture, runner: Runner, query: Callable[[], Any]) -> None:
    """Benchmark one call of the coroutine function `query`, after a warm-up call."""
    runner.run(query())
    benchmark(lambda: runner.run(query()))


# --- Reading one row ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "into",
    [None, Person, dict, PersonModel],
    ids=["tuple", "struct", "dict", "pydantic"],
)
def test_select_one(benchmark: BenchmarkFixture, runner: Runner, into: type[Any] | None) -> None:
    conn = runner.conn
    if into is None:
        _measure(benchmark, runner, lambda: conn.select_one(SELECT_ONE, 42))
    else:
        _measure(benchmark, runner, lambda: conn.select_one(SELECT_ONE, 42, into=into))


def test_select_one_scalar(benchmark: BenchmarkFixture, runner: Runner) -> None:
    conn = runner.conn
    _measure(benchmark, runner, lambda: conn.select_one("SELECT 1"))


def test_select_one_or_none_missing(benchmark: BenchmarkFixture, runner: Runner) -> None:
    conn = runner.conn
    _measure(
        benchmark,
        runner,
        lambda: conn.select_one_or_none(SELECT_ONE, -1, into=Person),
    )


# --- Reading many rows ----------------------------------------------------------------------


@pytest.mark.parametrize("rows", [100, 1_000])
@pytest.mark.parametrize(
    ("into", "strategy"),
    [
        (None, "auto"),
        (Person, "direct"),
        (Person, "msgpack"),
        (Person, "json"),
        (dict, "auto"),
        (PersonModel, "direct"),
        (PersonModel, "json"),
    ],
    ids=[
        "tuple",
        "struct-direct",
        "struct-msgpack",
        "struct-json",
        "dict",
        "pydantic-direct",
        "pydantic-json",
    ],
)
def test_select_many(
    benchmark: BenchmarkFixture,
    runner: Runner,
    rows: int,
    into: type[Any] | None,
    strategy: fastpg.Strategy,
) -> None:
    conn = runner.conn
    if into is None:
        _measure(benchmark, runner, lambda: conn.select_many(SELECT_RANGE, 1, rows))
    else:
        _measure(
            benchmark,
            runner,
            lambda: conn.select_many(SELECT_RANGE, 1, rows, into=into, strategy=strategy),
        )


def test_select_many_named_tuple(benchmark: BenchmarkFixture, runner: Runner) -> None:
    conn = runner.conn
    query = "SELECT id, name, score FROM people WHERE id >= $1 ORDER BY id LIMIT $2"
    _measure(benchmark, runner, lambda: conn.select_many(query, 1, 1_000, into=Row))


# --- Arrays ---------------------------------------------------------------------------------


def test_select_many_list_fields(benchmark: BenchmarkFixture, runner: Runner) -> None:
    conn = runner.conn
    query = (
        "SELECT id, ARRAY[name, 'tag-' || age] AS tags, ARRAY[id, age, 7] AS numbers"
        " FROM people WHERE id >= $1 ORDER BY id LIMIT $2"
    )
    _measure(benchmark, runner, lambda: conn.select_many(query, 1, 1_000, into=Tagged))


def test_select_many_any_list_param(benchmark: BenchmarkFixture, runner: Runner) -> None:
    conn = runner.conn
    query = f"SELECT {COLUMNS} FROM people WHERE id = ANY($1)"  # noqa: S608 - constant text
    ids = list(range(1, 2_000, 100))  # 20 ids
    _measure(benchmark, runner, lambda: conn.select_many(query, ids, into=Person))


def test_select_many_array_agg(benchmark: BenchmarkFixture, runner: Runner) -> None:
    conn = runner.conn
    # One-to-many: 1,000 people in 60 groups, each with its ids and names as lists.
    query = (
        "SELECT age, array_agg(id ORDER BY id) AS ids, array_agg(name ORDER BY id) AS names"
        " FROM people WHERE id <= $1 GROUP BY age ORDER BY age"
    )
    _measure(benchmark, runner, lambda: conn.select_many(query, 1_000, into=AgeGroup))


# --- Query forms: t-strings and named parameters --------------------------------------------


def test_select_one_template(benchmark: BenchmarkFixture, runner: Runner) -> None:
    conn = runner.conn
    person_id, min_age = 42, 18

    def query() -> Any:  # noqa: ANN401 - a coroutine
        return conn.select_one(
            t"SELECT {fastpg.Identifier('name')}, age FROM people "
            t"WHERE id = {person_id} AND age >= {min_age}",
        )

    _measure(benchmark, runner, query)


def test_select_one_named_params(benchmark: BenchmarkFixture, runner: Runner) -> None:
    conn = runner.conn
    query = "SELECT name, age FROM people WHERE id = :id AND age >= :min_age"
    params = ByAge(id=42, min_age=18)
    _measure(benchmark, runner, lambda: conn.select_one(query, params=params))


# --- Writes ---------------------------------------------------------------------------------


@pytest.fixture
def scratch(runner: Runner) -> str:
    """An empty temporary table shaped like part of `people` (dropped with the connection)."""
    runner.run(
        runner.conn.execute_script(
            "CREATE TEMP TABLE IF NOT EXISTS scratch "
            "(id integer, name text, score double precision, created_at timestamptz);"
            "TRUNCATE scratch",
        ),
    )
    return "scratch"


WRITE_ROWS = [
    (i, f"person-{i}", i / 10, dt.datetime(2024, 1, 1, tzinfo=dt.UTC)) for i in range(1_000)
]


def test_execute(benchmark: BenchmarkFixture, runner: Runner, scratch: str) -> None:
    conn = runner.conn
    query = f"INSERT INTO {scratch} VALUES ($1, $2, $3, $4)"  # noqa: S608 - a fixed name
    _measure(benchmark, runner, lambda: conn.execute(query, *WRITE_ROWS[0]))


def test_execute_many(benchmark: BenchmarkFixture, runner: Runner, scratch: str) -> None:
    conn = runner.conn
    query = f"INSERT INTO {scratch} VALUES ($1, $2, $3, $4)"  # noqa: S608 - a fixed name
    _measure(benchmark, runner, lambda: conn.execute_many(query, WRITE_ROWS))


def test_insert_many(benchmark: BenchmarkFixture, runner: Runner, scratch: str) -> None:
    conn = runner.conn
    _measure(benchmark, runner, lambda: conn.insert_many(scratch, WRITE_ROWS))


def test_copy_in(benchmark: BenchmarkFixture, runner: Runner, scratch: str) -> None:
    conn = runner.conn
    _measure(benchmark, runner, lambda: conn.copy_in(scratch, WRITE_ROWS))
