"""Schema changes under cached prepared statements, for every result target."""

import asyncio
from typing import NamedTuple, TypeIs

import msgspec
import pydantic
import pytest

import fastpg


class Pair(msgspec.Struct):
    a: int
    b: str


class PairTuple(NamedTuple):
    a: int
    b: str


class PairModel(pydantic.BaseModel):
    a: int
    b: str


class Numbers(msgspec.Struct):
    a: int
    b: int


type Target = tuple[type[object], fastpg.Strategy]


def _target_id(target: Target) -> str:
    into, strategy = target
    return f"{into.__name__}-{strategy}"


def _is_dict_row(row: object) -> TypeIs[dict[str, object]]:
    return isinstance(row, dict)


def _fields(row: object) -> dict[str, object]:
    """`a` and `b` of a row of any target."""
    if _is_dict_row(row):
        return {name: row[name] for name in ("a", "b")}
    return {name: getattr(row, name) for name in ("a", "b")}


@pytest.fixture(
    name="target",
    params=[
        (dict, "auto"),
        (Pair, "direct"),
        (Pair, "msgpack"),
        (Pair, "json"),
        (PairTuple, "auto"),
        (PairModel, "direct"),
        (PairModel, "json"),
    ],
    ids=_target_id,
)
def _target(request: pytest.FixtureRequest) -> Target:
    return request.param


@pytest.fixture(name="pairs")
async def _pairs(conn: fastpg.Connection) -> str:
    """A table `pairs (a int, b text)` holding (1, 'b'), and `SELECT * FROM pairs`."""
    await conn.execute_script(
        "CREATE TEMP TABLE pairs (a int, b text); INSERT INTO pairs VALUES (1, 'b')",
    )
    return "SELECT * FROM pairs"


async def _select(conn: fastpg.Connection, query: str, target: Target) -> list[object]:
    """Run `query` once for `target` (which prepares and caches it)."""
    into, strategy = target
    return await conn.select_many(query, into=into, strategy=strategy)


async def _add_column_and_select_in_a_transaction(conn: fastpg.Connection) -> None:
    async with conn.transaction() as tx:
        await tx.execute_script("ALTER TABLE changing ADD COLUMN b int")
        await tx.select_many("SELECT * FROM changing")


async def test_column_added(conn: fastpg.Connection, pairs: str, target: Target) -> None:
    [row] = await _select(conn, pairs, target)
    await conn.execute_script("ALTER TABLE pairs ADD COLUMN c int DEFAULT 3")
    [row] = await _select(conn, pairs, target)  # re-prepared: "SELECT *" has three columns
    assert _fields(row) == {"a": 1, "b": "b"}
    assert await conn.select_many(pairs) == [(1, "b", 3)]
    assert await conn.select_many(pairs, into=dict) == [{"a": 1, "b": "b", "c": 3}]


async def test_column_dropped(conn: fastpg.Connection, pairs: str, target: Target) -> None:
    await conn.execute_script("ALTER TABLE pairs ADD COLUMN c int DEFAULT 3")
    [row] = await _select(conn, pairs, target)
    await conn.execute_script("ALTER TABLE pairs DROP COLUMN c")
    [row] = await _select(conn, pairs, target)
    assert _fields(row) == {"a": 1, "b": "b"}
    assert await conn.select_many(pairs) == [(1, "b")]


@pytest.mark.parametrize(
    ("into", "strategy", "error"),
    [
        (Pair, "direct", fastpg.TypeMismatchError),
        (Pair, "msgpack", fastpg.DecodeError),
        (Pair, "json", fastpg.DecodeError),
        (PairTuple, "auto", fastpg.TypeMismatchError),
        (PairModel, "direct", fastpg.TypeMismatchError),
    ],
)
async def test_column_type_changed_rebinds_the_fields(
    conn: fastpg.Connection,
    pairs: str,
    into: type[object],
    strategy: fastpg.Strategy,
    error: type[fastpg.DecodeError],
) -> None:
    await conn.select_many(pairs, into=into, strategy=strategy)
    await conn.execute_script("ALTER TABLE pairs ALTER COLUMN a TYPE text")
    # The column-to-field binding cached with the statement was for int4: no stale reuse.
    with pytest.raises(error, match="a"):
        await conn.select_many(pairs, into=into, strategy=strategy)
    assert await conn.select_many(pairs) == [("1", "b")]
    assert await conn.select_many(pairs, into=dict) == [{"a": "1", "b": "b"}]


async def test_column_type_changed_is_coerced_by_pydantic_validation(
    conn: fastpg.Connection,
    pairs: str,
) -> None:
    await conn.select_many(pairs, into=PairModel, strategy="json")
    await conn.execute_script("ALTER TABLE pairs ALTER COLUMN a TYPE text")
    # Lax validation turns the text "1" into 1, where direct construction refuses text.
    assert await conn.select_many(pairs, into=PairModel, strategy="json") == [PairModel(a=1, b="b")]


@pytest.mark.parametrize("strategy", ["direct", "msgpack", "json"])
async def test_columns_of_one_type_swapping_names(
    conn: fastpg.Connection,
    strategy: fastpg.Strategy,
) -> None:
    await conn.execute_script(
        "CREATE TEMP TABLE swapped (a int, b int); INSERT INTO swapped VALUES (1, 2)",
    )
    query = "SELECT * FROM swapped"
    assert await conn.select_one(query, into=Numbers, strategy=strategy) == Numbers(1, 2)
    await conn.execute_script(
        "ALTER TABLE swapped RENAME COLUMN a TO c;"
        " ALTER TABLE swapped RENAME COLUMN b TO a;"
        " ALTER TABLE swapped RENAME COLUMN c TO b",
    )
    # Same column types, so only the names changed; values follow the names.
    assert await conn.select_one(query, into=Numbers, strategy=strategy) == Numbers(2, 1)
    assert await conn.select_one(query, into=dict) == {"b": 1, "a": 2}


async def test_table_dropped_and_recreated_with_columns_reordered(
    conn: fastpg.Connection,
    pairs: str,
    target: Target,
) -> None:
    await _select(conn, pairs, target)
    await conn.execute_script(
        "DROP TABLE pairs; CREATE TEMP TABLE pairs (b text, a int);"
        " INSERT INTO pairs VALUES ('c', 2)",
    )
    [row] = await _select(conn, pairs, target)
    assert _fields(row) == {"a": 2, "b": "c"}
    assert await conn.select_many(pairs) == [("c", 2)]


async def test_view_redefined(conn: fastpg.Connection, target: Target) -> None:
    await conn.execute_script("CREATE TEMP VIEW defined AS SELECT 1 AS a, 'b'::text AS b")
    query = "SELECT * FROM defined"
    await _select(conn, query, target)
    await conn.execute_script(
        "DROP VIEW defined; CREATE TEMP VIEW defined AS SELECT 'c'::text AS b, 2 AS a",
    )
    [row] = await _select(conn, query, target)
    assert _fields(row) == {"a": 2, "b": "c"}
    await conn.execute_script(
        "CREATE OR REPLACE TEMP VIEW defined AS SELECT 'd'::text AS b, 3 AS a, 4 AS c",
    )
    [row] = await _select(conn, query, target)
    assert _fields(row) == {"a": 3, "b": "d"}


async def test_every_pooled_connection_rebinds(dsn: str) -> None:
    async with await fastpg.create_pool(dsn, min_size=4, max_size=4) as pool:
        await pool.execute_script(
            "DROP TABLE IF EXISTS edge_pooled;"
            " CREATE TABLE edge_pooled (a int, b text); INSERT INTO edge_pooled VALUES (1, 'b')",
        )
        query = "SELECT * FROM edge_pooled"
        try:
            # Concurrent, so the statement is prepared and bound on several connections.
            rows = await asyncio.gather(*(pool.select_many(query, into=Pair) for _ in range(16)))
            assert rows == [[Pair(1, "b")]] * 16
            await pool.execute_script(
                "ALTER TABLE edge_pooled RENAME COLUMN a TO c;"
                " ALTER TABLE edge_pooled ADD COLUMN a int DEFAULT 5",
            )
            rows = await asyncio.gather(*(pool.select_many(query, into=Pair) for _ in range(16)))
            assert rows == [[Pair(5, "b")]] * 16
            assert pool.stats().size > 1
        finally:
            await pool.execute_script("DROP TABLE edge_pooled")


async def test_statements_deallocated_behind_the_cache(
    conn: fastpg.Connection,
    pairs: str,
    target: Target,
) -> None:
    await _select(conn, pairs, target)
    await conn.execute_script("DEALLOCATE ALL")
    [row] = await _select(conn, pairs, target)
    assert _fields(row) == {"a": 1, "b": "b"}


async def test_parameter_retyped_under_a_cached_statement(conn: fastpg.Connection) -> None:
    await conn.execute_script("CREATE TEMP TABLE retyped (x int); INSERT INTO retyped VALUES (5)")
    query = "SELECT x FROM retyped WHERE x = $1"
    assert await conn.select_many(query, 5) == [(5,)]
    await conn.execute_script("ALTER TABLE retyped ALTER COLUMN x TYPE text")
    # The cached statement still declares $1 as int4: a str is now right for it, so the
    # statement is prepared again (nothing was sent) and the query retried.
    assert await conn.select_many(query, "5") == [("5",)]


async def test_parameter_retyped_under_a_cached_statement_heals_after_one_error(
    conn: fastpg.Connection,
) -> None:
    await conn.execute_script("CREATE TEMP TABLE retyped (x int); INSERT INTO retyped VALUES (5)")
    query = "SELECT x FROM retyped WHERE x = $1"
    assert await conn.select_many(query, 5) == [(5,)]
    await conn.execute_script("ALTER TABLE retyped ALTER COLUMN x TYPE text")
    # The server re-plans the cached statement with $1 still int4 and finds no text = int4.
    with pytest.raises(fastpg.ProgrammingError, match="operator does not exist: text = integer"):
        await conn.select_many(query, 5)
    # That dropped it from the cache: prepared afresh, $1 is now text.
    with pytest.raises(fastpg.ParameterError, match="cannot encode int as PostgreSQL type text"):
        await conn.select_many(query, 5)
    assert await conn.select_many(query, "5") == [("5",)]


async def test_stale_statement_in_a_transaction_reports_the_schema_change(
    conn: fastpg.Connection,
) -> None:
    await conn.execute_script("CREATE TEMP TABLE changing (a int); INSERT INTO changing VALUES (1)")
    assert await conn.select_many("SELECT * FROM changing") == [(1,)]
    # The server's error aborts the transaction, so the statement can't be re-prepared in
    # it: the error is the one that says why, not "current transaction is aborted".
    with pytest.raises(fastpg.NotSupportedError, match="cached plan must not change result type"):
        await _add_column_and_select_in_a_transaction(conn)
    assert await conn.select_many("SELECT * FROM changing") == [(1,)]
