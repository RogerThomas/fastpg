"""Schema changes under cached prepared statements, for every result target."""

import pytest

import fastpg


async def _add_column_and_select_in_a_transaction(conn: fastpg.Connection) -> None:
    async with conn.transaction() as tx:
        await tx.execute_script("ALTER TABLE changing ADD COLUMN b int")
        await tx.select_many("SELECT * FROM changing")


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
