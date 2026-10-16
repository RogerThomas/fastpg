"""Schema changes under cached prepared statements, for every result target."""

import pytest

import fastpg


async def test_stale_statement_in_a_transaction_reports_the_schema_change(
    conn: fastpg.Connection,
) -> None:
    await conn.execute_script("CREATE TEMP TABLE changing (a int); INSERT INTO changing VALUES (1)")
    assert await conn.select_many("SELECT * FROM changing") == [(1,)]
    # The server's error aborts the transaction, so the statement can't be re-prepared in
    # it: the error is the one that says why, not "current transaction is aborted".
    with pytest.raises(fastpg.NotSupportedError, match="cached plan must not change result type"):
        async with conn.transaction() as tx:
            await tx.execute_script("ALTER TABLE changing ADD COLUMN b int")
            await tx.select_many("SELECT * FROM changing")
    assert await conn.select_many("SELECT * FROM changing") == [(1,)]
