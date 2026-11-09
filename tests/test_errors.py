"""The exception hierarchy lines up with DB-API 2.0 (PEP 249)."""

import inspect
import pickle

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
