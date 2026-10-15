"""The protocol on the event-loop thread: queries, writes, cancellation and the server going
away, end to end."""

import asyncio
import gc
import uuid
import warnings

import msgspec
import pytest
import uvloop

import fastpg
from fastpg import errors


class Person(msgspec.Struct):
    id: int
    name: str
    email: str | None
    external_id: uuid.UUID


async def test_select_shapes(conn: fastpg.Connection) -> None:
    assert await conn.select_one("SELECT 1, 'a'") == (1, "a")
    with pytest.raises(fastpg.NoRowsError):
        await conn.select_one("SELECT 1 WHERE false")
    with pytest.raises(fastpg.TooManyRowsError):
        await conn.select_one("SELECT generate_series(1, 2)")
    assert await conn.select_one_or_none("SELECT 1 WHERE false") is None
    assert await conn.select_many("SELECT 1 WHERE false") == []
    assert await conn.select_many("SELECT generate_series(1, 3)") == [(1,), (2,), (3,)]


async def test_execute_returns_rowcount(conn: fastpg.Connection) -> None:
    await conn.execute_script(
        "DROP TABLE IF EXISTS loop_io_exec_test;"
        " CREATE TABLE loop_io_exec_test (id int PRIMARY KEY, v text);",
    )
    sql = "INSERT INTO loop_io_exec_test VALUES ($1, $2), ($3, $4)"
    assert await conn.execute(sql, 1, "a", 2, None) == 2
    assert await conn.execute("UPDATE loop_io_exec_test SET v = $1", "z") == 2
    rows = await conn.select_many("SELECT * FROM loop_io_exec_test ORDER BY id")
    assert rows == [(1, "z"), (2, "z")]
    await conn.execute_script("DROP TABLE loop_io_exec_test")


async def test_nulls_and_structs(conn: fastpg.Connection) -> None:
    assert await conn.select_one("SELECT NULL::text, $1::int", None) == (None, None)
    sql = "SELECT id, name, email, external_id FROM people WHERE id = $1"
    person = await conn.select_one(sql, 7, into=Person)
    assert person == Person(7, "person-7", None, person.external_id)
    assert (await conn.select_one(sql, 8, into=Person)).email == "person8@example.com"
    for strategy in ("msgpack", "json"):
        assert await conn.select_one(sql, 7, into=Person, strategy=strategy) == person
    rows = await conn.select_many(
        "SELECT id, name, email, external_id FROM people WHERE id <= $1 ORDER BY id",
        3,
        into=Person,
    )
    assert [p.id for p in rows] == [1, 2, 3]


async def test_server_error_keeps_connection_usable(conn: fastpg.Connection) -> None:
    with pytest.raises(errors.SQLSyntaxError) as exc:
        await conn.select_one("SELEC 1")
    assert exc.value.sqlstate == "42601"
    with pytest.raises(fastpg.ParameterError, match="expects 1 parameter"):
        await conn.select_one("SELECT $1::int")
    assert await conn.select_one("SELECT 1") == (1,)


async def test_large_results_are_streamed(conn: fastpg.Connection) -> None:
    rows = await conn.select_many(
        "SELECT id, name FROM people WHERE id <= $1 ORDER BY id",
        20_000,
    )
    assert len(rows) == 20_000
    assert rows[0] == (1, "person-1")
    assert rows[-1] == (20_000, "person-20000")


@pytest.mark.parametrize("width", [15, 16, 17, 40])
async def test_rows_of_any_width_with_nulls(conn: fastpg.Connection, width: int) -> None:
    # Rows of up to 16 columns are buffered in one pass, wider ones in two; 600 rows are
    # streamed in two chunks.
    nulls = [i % 3 == 0 for i in range(width)]
    columns = ", ".join(
        f"NULL::int4 AS c{i}" if null else f"n + {i} AS c{i}" for i, null in enumerate(nulls)
    )
    sql = f"SELECT {columns} FROM generate_series(1, $1) AS n"  # noqa: S608 - fixed text
    rows = await conn.select_many(sql, 600)
    assert len(rows) == 600
    assert rows[-1] == tuple(None if null else 600 + i for i, null in enumerate(nulls))
    one = await conn.select_one(sql, 1)
    assert one == tuple(None if null else 1 + i for i, null in enumerate(nulls))


async def test_results_of_different_shapes_on_one_connection(conn: fastpg.Connection) -> None:
    # Each result's row buffer is reused by the next ones.
    sql = "SELECT n, repeat('é', n % 7), NULL::text, n::float8 FROM generate_series(1, $1) AS n"
    first = await conn.select_many(sql, 2000)
    assert first[-1] == (2000, "é" * (2000 % 7), None, 2000.0)
    assert await conn.select_one("SELECT 'a', NULL::int4, 'ü✓'") == ("a", None, "ü✓")
    assert await conn.select_one("SELECT 1") == (1,)
    assert await conn.select_many(sql, 2000) == first
    assert await conn.select_many(sql, 3) == first[:3]


@pytest.mark.parametrize("size", [16_383, 16_384, 16_385, 65_536, 500_000])
@pytest.mark.parametrize("char", ["x", "é"])
async def test_values_around_the_socket_read_size(
    conn: fastpg.Connection,
    size: int,
    char: str,
) -> None:
    # The socket is read 16 KiB at a time.
    assert await conn.select_one("SELECT repeat($2, $1), $1", size, char) == (char * size, size)


async def test_pipelined_results_of_mixed_sizes(conn: fastpg.Connection) -> None:
    sizes = [1, 20_000, 3, 200_000, 0, 16_384, 10]
    results = await asyncio.gather(
        *(conn.select_one("SELECT repeat('x', $1), $1", n) for n in sizes),
    )
    assert results == [("x" * n, n) for n in sizes]


async def test_concurrent_queries_are_pipelined(conn: fastpg.Connection) -> None:
    ids = list(range(1, 201))
    results = await asyncio.gather(
        *(conn.select_one("SELECT id, name FROM people WHERE id = $1", i) for i in ids),
    )
    assert results == [(i, f"person-{i}") for i in ids]


async def test_transactions_and_bulk_writes(conn: fastpg.Connection) -> None:
    await conn.execute_script(
        "DROP TABLE IF EXISTS loop_io_bulk_test; CREATE TABLE loop_io_bulk_test (id int, v text);",
    )
    async with conn.transaction() as tx:
        sql = "INSERT INTO loop_io_bulk_test VALUES ($1, $2)"
        assert await tx.execute_many(sql, [(1, "a"), (2, "b")]) == 2
    copied = await conn.copy_in("loop_io_bulk_test", [(i, str(i)) for i in range(3, 10_003)])
    assert copied == 10_000
    totals = await conn.select_one("SELECT count(*), max(id) FROM loop_io_bulk_test")
    assert totals == (10_002, 10_002)
    await conn.execute_script("DROP TABLE loop_io_bulk_test")


async def test_cancelled_query_does_not_break_connection(conn: fastpg.Connection) -> None:
    task = asyncio.create_task(conn.select_one("SELECT pg_sleep(0.2)"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await conn.select_one("SELECT 2") == (2,)


async def test_close_is_graceful_and_releases_the_socket(dsn: str) -> None:
    conn = await fastpg.connect(dsn)
    in_flight = asyncio.create_task(conn.select_one("SELECT pg_sleep(0.1), 1"))
    await asyncio.sleep(0.01)
    await conn.close()
    assert (await in_flight)[1] == 1
    assert conn.closed
    with pytest.raises(fastpg.ConnectionClosedError):
        await conn.select_one("SELECT 1")
    # The database socket was closed by fastpg (Rust detached it, then Python closed it).
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        del conn
        gc.collect()
    assert not [w for w in caught if "unclosed <socket" in str(w.message)]


async def test_server_terminating_connection(conn: fastpg.Connection, dsn: str) -> None:
    victim = await fastpg.connect(dsn)
    (pid,) = await victim.select_one("SELECT pg_backend_pid()")
    await conn.select_one("SELECT pg_terminate_backend($1)", pid)
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
    sleeping = [asyncio.create_task(victim.select_one("SELECT pg_sleep(30)")) for _ in range(3)]
    await asyncio.sleep(0.1)
    await conn.select_one("SELECT pg_terminate_backend($1)", pid)
    async with asyncio.timeout(5):  # must fail promptly, never hang
        for task in sleeping:
            with pytest.raises((errors.OperationalError, fastpg.ConnectionClosedError)):
                await task
    await victim.close()


async def test_cancelled_connect_does_not_leak(conn: fastpg.Connection, dsn: str) -> None:
    tagged = f"{dsn}?application_name=fastpg_loop_io_cancel"
    for _ in range(5):
        task = asyncio.create_task(fastpg.connect(tagged))
        await asyncio.sleep(0)
        task.cancel()
        try:
            opened = await task
        except asyncio.CancelledError:
            continue
        await opened.close()  # connected before the cancel landed
    sql = "SELECT count(*) FROM pg_stat_activity WHERE application_name = 'fastpg_loop_io_cancel'"
    async with asyncio.timeout(5):
        while (await conn.select_one(sql))[0]:
            await asyncio.sleep(0.05)


async def test_bad_credentials(dsn: str) -> None:
    with pytest.raises(fastpg.DatabaseError) as exc:
        await fastpg.connect(dsn.replace("fastpg:fastpg@", "fastpg:wrong@"))
    assert exc.value.sqlstate == "28P01"


async def test_unreachable_server() -> None:
    with pytest.raises(fastpg.InterfaceError, match="could not connect"):
        await fastpg.connect("postgresql://fastpg:fastpg@127.0.0.1:1/fastpg")


def test_works_under_uvloop(dsn: str) -> None:
    async def run() -> tuple[object, ...]:
        async with await fastpg.connect(dsn) as conn:
            return await conn.select_one("SELECT 'uv'")

    assert uvloop.run(run()) == ("uv",)
