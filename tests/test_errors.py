"""The exception hierarchy lines up with DB-API 2.0 (PEP 249)."""

import inspect
import pickle
import secrets
from typing import LiteralString, cast
from urllib.parse import urlsplit

import pytest

import fastpg
from fastpg import errors


def test_error_is_fastpg_error() -> None:
    assert fastpg.Error is fastpg.FastPGError


@pytest.mark.parametrize(
    ("error", "parent"),
    [
        (fastpg.Error, Exception),
        (fastpg.InterfaceError, fastpg.Error),
        (fastpg.DatabaseError, fastpg.Error),
        (fastpg.DataError, fastpg.DatabaseError),
        (fastpg.OperationalError, fastpg.DatabaseError),
        (fastpg.IntegrityError, fastpg.DatabaseError),
        (fastpg.InternalError, fastpg.DatabaseError),
        (fastpg.ProgrammingError, fastpg.DatabaseError),
        (fastpg.NotSupportedError, fastpg.DatabaseError),
        (fastpg.ConnectError, fastpg.OperationalError),
        (fastpg.ConnectionClosedError, fastpg.OperationalError),
        (fastpg.AuthenticationError, fastpg.OperationalError),
    ],
)
def test_pep_249_hierarchy(error: type[Exception], parent: type[Exception]) -> None:
    assert issubclass(error, parent)


def test_pep_249_names_are_exported() -> None:
    names = {
        "Error",
        "InterfaceError",
        "DatabaseError",
        "DataError",
        "OperationalError",
        "IntegrityError",
        "InternalError",
        "ProgrammingError",
        "NotSupportedError",
    }
    assert names <= set(fastpg.__all__)


@pytest.mark.parametrize("error", [fastpg.ConnectError, fastpg.ConnectionClosedError])
def test_connection_failures_are_not_interface_errors(error: type[Exception]) -> None:
    assert not issubclass(error, fastpg.InterfaceError)


def test_every_error_class_is_exported() -> None:
    classes = {
        name: cls
        for name, cls in vars(errors).items()
        if inspect.isclass(cls) and issubclass(cls, fastpg.FastPGError)
    }
    assert set(classes) <= set(fastpg.__all__)
    for name, cls in classes.items():
        assert getattr(fastpg, name) is cls


def test_an_error_that_never_reached_the_server_has_no_server_fields() -> None:
    error = fastpg.ConnectError("could not connect to server: refused")
    assert error.sqlstate is None
    assert error.severity is None
    assert error.detail is None
    assert error.hint is None
    assert str(error) == "could not connect to server: refused"
    assert repr(error) == "ConnectError('could not connect to server: refused')"


@pytest.mark.parametrize(
    "error",
    [
        fastpg.ConnectError("could not connect to server: refused"),
        fastpg.ConnectionClosedError("connection lost"),
        fastpg.AuthenticationError(
            'password authentication failed for user "x"',
            sqlstate="28P01",
            severity="FATAL",
        ),
    ],
    ids=lambda e: type(e).__name__,
)
def test_pickling_keeps_the_class_and_fields(error: fastpg.DatabaseError) -> None:
    unpickled = pickle.loads(pickle.dumps(error))  # noqa: S301 - pickled just above
    assert type(unpickled) is type(error)
    assert unpickled.args == error.args
    assert unpickled.sqlstate == error.sqlstate
    assert unpickled.severity == error.severity
    assert str(unpickled) == str(error)


# Every SQLSTATE class PostgreSQL defines (https://www.postgresql.org/docs/current/errcodes-appendix.html),
# and the class it raises: psycopg 3's PEP 249 mapping, except 28 and 40.
SQLSTATE_CLASSES: list[tuple[str, type[fastpg.DatabaseError]]] = [
    ("03", fastpg.DatabaseError),  # SQL statement not yet complete
    ("08", fastpg.OperationalError),  # connection exception
    ("09", fastpg.DatabaseError),  # triggered action exception
    ("0A", fastpg.NotSupportedError),  # feature not supported
    ("0B", fastpg.DatabaseError),  # invalid transaction initiation
    ("0F", fastpg.DatabaseError),  # locator exception
    ("0L", fastpg.DatabaseError),  # invalid grantor
    ("0P", fastpg.DatabaseError),  # invalid role specification
    ("0Z", fastpg.DatabaseError),  # diagnostics exception
    ("10", fastpg.ProgrammingError),  # XQuery error
    ("20", fastpg.ProgrammingError),  # case not found
    ("21", fastpg.ProgrammingError),  # cardinality violation
    ("22", fastpg.DataError),  # data exception
    ("23", fastpg.IntegrityError),  # integrity constraint violation
    ("24", fastpg.InternalError),  # invalid cursor state
    ("25", fastpg.InternalError),  # invalid transaction state
    ("26", fastpg.ProgrammingError),  # invalid SQL statement name
    ("27", fastpg.OperationalError),  # triggered data change violation
    ("28", fastpg.AuthenticationError),  # invalid authorization specification
    ("2B", fastpg.InternalError),  # dependent privilege descriptors still exist
    ("2D", fastpg.InternalError),  # invalid transaction termination
    ("2F", fastpg.OperationalError),  # SQL routine exception
    ("34", fastpg.ProgrammingError),  # invalid cursor name
    ("38", fastpg.OperationalError),  # external routine exception
    ("39", fastpg.OperationalError),  # external routine invocation exception
    ("3B", fastpg.OperationalError),  # savepoint exception
    ("3D", fastpg.ProgrammingError),  # invalid catalog name
    ("3F", fastpg.ProgrammingError),  # invalid schema name
    ("40", fastpg.TransactionError),  # transaction rollback
    ("42", fastpg.ProgrammingError),  # syntax error or access rule violation
    ("44", fastpg.ProgrammingError),  # WITH CHECK OPTION violation
    ("53", fastpg.OperationalError),  # insufficient resources
    ("54", fastpg.OperationalError),  # program limit exceeded
    ("55", fastpg.OperationalError),  # object not in prerequisite state
    ("57", fastpg.OperationalError),  # operator intervention
    ("58", fastpg.OperationalError),  # system error
    ("72", fastpg.DatabaseError),  # snapshot failure
    ("F0", fastpg.OperationalError),  # configuration file error
    ("HV", fastpg.OperationalError),  # foreign data wrapper error
    ("P0", fastpg.ProgrammingError),  # PL/pgSQL error
    ("XX", fastpg.InternalError),  # internal error
]


def _error_class(sqlstate: str) -> type[fastpg.DatabaseError]:
    return errors._SQLStateRegistry.error_class(sqlstate)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(("sqlstate_class", "expected"), SQLSTATE_CLASSES)
def test_every_sqlstate_class_raises_its_class(
    sqlstate_class: str,
    expected: type[fastpg.DatabaseError],
) -> None:
    assert _error_class(f"{sqlstate_class}000") is expected
    assert _error_class(f"{sqlstate_class}P99") is expected


@pytest.mark.parametrize(
    ("sqlstate", "expected"),
    [
        ("23502", fastpg.NotNullViolationError),
        ("23503", fastpg.ForeignKeyViolationError),
        ("23505", fastpg.UniqueViolationError),
        ("23514", fastpg.CheckViolationError),
        ("40001", fastpg.SerializationError),
        ("40P01", fastpg.DeadlockDetectedError),
        ("42601", fastpg.SQLSyntaxError),
        ("42703", fastpg.UndefinedColumnError),
        ("42P01", fastpg.UndefinedTableError),
        ("57014", fastpg.QueryCanceledError),
    ],
)
def test_codes_with_a_class_of_their_own(
    sqlstate: str,
    expected: type[fastpg.DatabaseError],
) -> None:
    assert _error_class(sqlstate) is expected


async def test_a_missing_database_is_a_programming_error(dsn: str) -> None:
    missing = urlsplit(dsn)._replace(path="/fastpg_no_such_database").geturl()
    with pytest.raises(fastpg.ProgrammingError) as exc:
        await fastpg.connect(missing)
    assert exc.value.sqlstate == "3D000"


async def test_raise_exception_is_a_programming_error(conn: fastpg.Connection) -> None:
    with pytest.raises(fastpg.ProgrammingError, match="x") as exc:
        await conn.execute("DO $$ BEGIN RAISE EXCEPTION 'x'; END $$")
    assert exc.value.sqlstate == "P0001"


async def test_nowait_on_a_locked_row_is_an_operational_error(
    dsn: str,
    conn: fastpg.Connection,
) -> None:
    # A table of its own (not TEMP: the other session must see it), named for this test run.
    table = cast("LiteralString", f"errors_lock_{secrets.token_hex(4)}")
    await conn.execute_script(f"CREATE TABLE {table} (id int); INSERT INTO {table} VALUES (1)")  # noqa: S608
    try:
        async with await fastpg.connect(dsn) as other:
            async with conn.transaction() as tx:
                await tx.execute(f"SELECT id FROM {table} FOR UPDATE")  # noqa: S608
                with pytest.raises(fastpg.OperationalError) as exc:
                    await other.execute(f"SELECT id FROM {table} FOR UPDATE NOWAIT")  # noqa: S608
            assert exc.value.sqlstate == "55P03"
    finally:
        await conn.execute_script(f"DROP TABLE {table}")


async def _go_on_after_an_error(conn: fastpg.Connection) -> None:
    """Swallow a server error inside the block, try another query, then exit normally."""
    async with conn.transaction() as tx:
        with pytest.raises(fastpg.DataError):
            await tx.select_one("SELECT 1 / 0")
        with pytest.raises(fastpg.InternalError) as exc:
            await tx.select_one("SELECT 1")
        assert exc.value.sqlstate == "25P02"


async def test_an_aborted_transaction_is_an_internal_error(conn: fastpg.Connection) -> None:
    # The block exits normally, so its COMMIT fails with 25P02 too.
    with pytest.raises(fastpg.InternalError) as exc:
        await _go_on_after_an_error(conn)
    assert exc.value.sqlstate == "25P02"
