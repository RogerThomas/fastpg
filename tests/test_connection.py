"""Connection behaviour: result shapes, errors, parameters, concurrency, lifecycle."""

import asyncio
import datetime as dt
import time

import pytest
import uvloop

import fastpg
from fastpg import errors


async def test_select_one_shapes(conn: fastpg.Connection) -> None:
    assert await conn.select_one("SELECT 1, 'a'") == (1, "a")
    with pytest.raises(fastpg.NoRowsError):
        await conn.select_one("SELECT 1 WHERE false")
    with pytest.raises(fastpg.TooManyRowsError):
        await conn.select_one("SELECT generate_series(1, 2)")
    assert await conn.select_one_or_none("SELECT 1 WHERE false") is None
    with pytest.raises(fastpg.TooManyRowsError):
        await conn.select_one_or_none("SELECT generate_series(1, 2)")
    assert await conn.select_many("SELECT 1 WHERE false") == []
    assert await conn.select_many("SELECT generate_series(1, 3)") == [(1,), (2,), (3,)]


async def test_no_rows_error_is_lookup_error(conn: fastpg.Connection) -> None:
    with pytest.raises(LookupError):
        await conn.select_one("SELECT 1 WHERE false")


async def test_execute_returns_rowcount(conn: fastpg.Connection) -> None:
    await conn.execute_script(
        "DROP TABLE IF EXISTS exec_test; CREATE TABLE exec_test (id int PRIMARY KEY, v text);",
    )
    inserted = await conn.execute(
        "INSERT INTO exec_test VALUES ($1, $2), ($3, $4)",
        1,
        "a",
        2,
        None,
    )
    assert inserted == 2
    assert await conn.execute("UPDATE exec_test SET v = $1", "z") == 2
    assert await conn.select_many("SELECT * FROM exec_test ORDER BY id") == [(1, "z"), (2, "z")]
    await conn.execute_script("DROP TABLE exec_test")


async def test_unique_violation(conn: fastpg.Connection) -> None:
    await conn.execute_script(
        "DROP TABLE IF EXISTS uniq_test;"
        " CREATE TABLE uniq_test (id int CONSTRAINT uniq_pk PRIMARY KEY);",
    )
    await conn.execute("INSERT INTO uniq_test VALUES ($1)", 1)
    with pytest.raises(fastpg.UniqueViolationError) as exc:
        await conn.execute("INSERT INTO uniq_test VALUES ($1)", 1)
    err = exc.value
    assert isinstance(err, fastpg.IntegrityError)
    assert err.sqlstate == "23505"
    assert err.constraint_name == "uniq_pk"
    assert err.table_name == "uniq_test"
    assert err.detail is not None
    assert "already exists" in err.detail
    await conn.execute_script("DROP TABLE uniq_test")


@pytest.mark.parametrize(
    ("sql", "error", "sqlstate"),
    [
        ("SELEC 1", errors.SQLSyntaxError, "42601"),
        ("SELECT * FROM no_such_table", errors.UndefinedTableError, "42P01"),
        ("SELECT no_such_column FROM people", errors.UndefinedColumnError, "42703"),
        ("SELECT 1 / 0", errors.DataError, "22012"),
        ("SELECT count(*) FROM people FOR UPDATE", fastpg.NotSupportedError, "0A000"),
    ],
)
async def test_server_errors_map_to_classes(
    conn: fastpg.Connection,
    sql: str,
    error: type[fastpg.DatabaseError],
    sqlstate: str,
) -> None:
    with pytest.raises(error) as exc:
        await conn.select_one(sql)
    assert exc.value.sqlstate == sqlstate
    # The connection stays usable after a server error.
    assert await conn.select_one("SELECT 1") == (1,)


async def test_parameter_errors(conn: fastpg.Connection) -> None:
    with pytest.raises(fastpg.ParameterError, match="expects 1 parameter"):
        await conn.select_one("SELECT $1::int")
    with pytest.raises(fastpg.ParameterError, match=r"\$1: cannot encode str as .* int4"):
        await conn.select_one("SELECT $1::int", "1")
    with pytest.raises(fastpg.ParameterError, match="out of range for PostgreSQL type int2"):
        await conn.select_one("SELECT $1::int2", 70_000)
    with pytest.raises(fastpg.ParameterError, match="does not fit in 64 bits"):
        await conn.select_one("SELECT $1::int8", 2**64)
    with pytest.raises(fastpg.ParameterError, match="naive datetime"):
        await conn.select_one("SELECT $1::timestamptz", dt.datetime(2024, 1, 1))  # noqa: DTZ001
    assert await conn.select_one("SELECT $1::int", 1) == (1,)


async def test_concurrent_queries_are_pipelined(conn: fastpg.Connection) -> None:
    ids = list(range(1, 501))
    results = await asyncio.gather(
        *(conn.select_one("SELECT id, name FROM people WHERE id = $1", i) for i in ids),
    )
    assert results == [(i, f"person-{i}") for i in ids]


async def _two_queries(conn: fastpg.Connection, task: int) -> list[object]:
    """Two queries in a row; the second divides by zero in even tasks."""
    results: list[object] = [await conn.select_one("SELECT $1::int", task)]
    try:
        results.append(await conn.select_one("SELECT $1::int / $2::int", task, task % 2))
    except errors.DataError as e:
        results.append(e.sqlstate)
    return results


async def _hold_the_loop() -> list[object]:
    """Block the loop once, so that every pending result has arrived when it next reads."""
    time.sleep(0.05)
    return []


async def test_queries_sent_after_results_arrive_together(conn: fastpg.Connection) -> None:
    # All 16 first results are read at once and wake all 16 tasks, whose second queries are
    # then written together by one flush. Each task must still get its own result or error
    # (a flush that never ran would hang here). Both statements are prepared first, so no
    # other traffic is in flight to write the queued queries by chance.
    assert await _two_queries(conn, 1) == [(1,), (1,)]
    async with asyncio.timeout(10):
        *results, _ = await asyncio.gather(
            *(_two_queries(conn, task) for task in range(16)),
            _hold_the_loop(),
        )
    assert results == [[(task,), "22012" if task % 2 == 0 else (task,)] for task in range(16)]


async def test_statement_cache_is_bounded(dsn: str) -> None:
    async with await fastpg.connect(dsn, statement_cache_size=2) as conn:
        for i in range(5):
            assert await conn.select_one(f"SELECT {i}") == (i,)
        assert conn.cached_statements == 2
        # Evicted statements were deallocated server-side and re-prepare transparently.
        assert await conn.select_one("SELECT 0") == (0,)
        (prepared,) = await conn.select_one("SELECT count(*) FROM pg_prepared_statements")
        assert prepared <= 3


async def test_statement_reprepared_after_ddl_changes_result(conn: fastpg.Connection) -> None:
    await conn.execute_script(
        "DROP TABLE IF EXISTS ddl_test;"
        " CREATE TABLE ddl_test (a int); INSERT INTO ddl_test VALUES (1);",
    )
    assert await conn.select_many("SELECT * FROM ddl_test") == [(1,)]
    await conn.execute_script("ALTER TABLE ddl_test ADD COLUMN b text DEFAULT 'x'")
    # The cached statement now fails with "cached plan must not change result type";
    # fastpg re-prepares transparently.
    assert await conn.select_many("SELECT * FROM ddl_test") == [(1, "x")]
    await conn.execute_script("DROP TABLE ddl_test")


async def test_statement_reprepared_after_deallocate_all(conn: fastpg.Connection) -> None:
    assert await conn.select_one("SELECT $1::int + 1", 1) == (2,)
    await conn.execute_script("DEALLOCATE ALL")
    assert await conn.select_one("SELECT $1::int + 1", 2) == (3,)
    assert await conn.execute("SELECT $1::int", 1) == 1


async def test_cancelled_connect_does_not_leak(conn: fastpg.Connection, dsn: str) -> None:
    tagged = f"{dsn}?application_name=fastpg_cancel_test"
    for _ in range(5):
        task = asyncio.create_task(fastpg.connect(tagged))
        await asyncio.sleep(0)
        task.cancel()
        try:
            opened = await task
        except asyncio.CancelledError:
            continue
        await opened.close()  # connected before the cancel landed
    sql = "SELECT count(*) FROM pg_stat_activity WHERE application_name = 'fastpg_cancel_test'"
    async with asyncio.timeout(5):
        while (await conn.select_one(sql))[0]:
            await asyncio.sleep(0.05)


async def test_cancelled_query_does_not_break_connection(conn: fastpg.Connection) -> None:
    task = asyncio.create_task(conn.select_one("SELECT pg_sleep(0.2)"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await conn.select_one("SELECT 2") == (2,)


async def test_close_is_graceful_and_final(dsn: str) -> None:
    conn = await fastpg.connect(dsn)
    in_flight = asyncio.create_task(conn.select_one("SELECT pg_sleep(0.1), 1"))
    await asyncio.sleep(0.01)
    await conn.close()
    assert (await in_flight)[1] == 1
    assert conn.closed
    with pytest.raises(fastpg.ConnectionClosedError):
        await conn.select_one("SELECT 1")


async def test_close_while_a_statement_is_being_prepared(dsn: str) -> None:
    conn = await fastpg.connect(dsn)
    in_flight = asyncio.create_task(conn.select_one("SELECT $1::int + 1", 1))
    await asyncio.sleep(0)  # sent: its Parse is on the way, not answered yet
    async with asyncio.timeout(5):  # the prepared statement must not keep the session open
        await conn.close()
    assert await in_flight == (2,)
    assert conn.closed


async def test_unclosed_connection_warns_and_cleans_up(conn: fastpg.Connection, dsn: str) -> None:
    leaked = await fastpg.connect(dsn)
    (pid,) = await leaked.select_one("SELECT pg_backend_pid()")
    with pytest.warns(ResourceWarning, match="unclosed fastpg connection"):
        del leaked  # refcounting frees it right here, running __del__
    # The session was closed: its backend goes away.
    sql = "SELECT count(*) FROM pg_stat_activity WHERE pid = $1"
    async with asyncio.timeout(5):
        while (await conn.select_one(sql, pid))[0]:
            await asyncio.sleep(0.01)


async def test_server_terminating_connection(conn: fastpg.Connection, dsn: str) -> None:
    victim = await fastpg.connect(dsn)
    (pid,) = await victim.select_one("SELECT pg_backend_pid()")
    await conn.select_one("SELECT pg_terminate_backend($1)", pid)
    # Depending on timing the next query sees the server's "terminating connection" error
    # (57P01) or the already-closed socket.
    with pytest.raises((errors.OperationalError, fastpg.ConnectionClosedError)):
        await victim.select_one("SELECT 1")
    async with asyncio.timeout(5):
        while not victim.closed:
            await asyncio.sleep(0.01)
    with pytest.raises(fastpg.ConnectionClosedError):
        await victim.select_one("SELECT 1")
    await victim.close()
    assert victim.closed


async def test_in_flight_query_fails_when_server_closes(conn: fastpg.Connection, dsn: str) -> None:
    victim = await fastpg.connect(dsn)
    (pid,) = await victim.select_one("SELECT pg_backend_pid()")
    sleeping = asyncio.create_task(victim.select_one("SELECT pg_sleep(30)"))
    await asyncio.sleep(0.1)
    await conn.select_one("SELECT pg_terminate_backend($1)", pid)
    async with asyncio.timeout(5):  # must fail promptly, never hang
        with pytest.raises((errors.OperationalError, fastpg.ConnectionClosedError)):
            await sleeping
    await victim.close()


async def test_pipelined_and_racing_queries_fail_when_server_closes(
    conn: fastpg.Connection,
    dsn: str,
) -> None:
    # Only the running query receives the server's FATAL error; the ones pipelined behind
    # it, and one sent right after it (before EOF is seen), must fail too, not hang.
    victim = await fastpg.connect(dsn)
    (pid,) = await victim.select_one("SELECT pg_backend_pid()")
    sleeping = [asyncio.create_task(victim.select_one("SELECT pg_sleep(30)")) for _ in range(3)]
    await asyncio.sleep(0.1)
    await conn.select_one("SELECT pg_terminate_backend($1)", pid)
    async with asyncio.timeout(5):
        done, _ = await asyncio.wait(sleeping, return_when=asyncio.FIRST_COMPLETED)
        with pytest.raises(fastpg.ConnectionClosedError):
            await victim.select_one("SELECT 1")
        for task in sleeping:
            with pytest.raises((errors.OperationalError, fastpg.ConnectionClosedError)):
                await task
    assert done
    await victim.close()


async def test_bad_credentials(dsn: str) -> None:
    with pytest.raises(fastpg.DatabaseError) as exc:
        await fastpg.connect(dsn.replace("fastpg:fastpg@", "fastpg:wrong@"))
    assert exc.value.sqlstate == "28P01"


async def test_unreachable_server() -> None:
    with pytest.raises(fastpg.InterfaceError):
        await fastpg.connect("postgresql://fastpg:fastpg@127.0.0.1:1/fastpg")


def test_works_under_uvloop(dsn: str) -> None:
    async def run() -> tuple[object, ...]:
        async with await fastpg.connect(dsn) as conn:
            return await conn.select_one("SELECT 'uv'")

    assert uvloop.run(run()) == ("uv",)
