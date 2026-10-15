"""The exception hierarchy lines up with DB-API 2.0 (PEP 249)."""

import pytest

import fastpg


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
