"""Exception hierarchy.

Every DB-API 2.0 (PEP 249) exception name but ``Warning`` is here, in PEP 249's place:
``Error`` is an alias of ``FastPGError``; ``InterfaceError``, ``DatabaseError`` and the six
classes under it are the classes themselves. ``Warning`` has no counterpart, because fastpg
never raises one.

FastPGError (alias: Error)
├── InterfaceError                client-side problems
│   ├── ConnectionClosedError
│   └── ParameterError            parameter missing, or can't be encoded for its declared type
├── ResultError                   result doesn't fit what the caller asked for
│   ├── NoRowsError               select_one() found nothing
│   ├── TooManyRowsError          select_one() / select_one_or_none() found >1 row
│   └── DecodeError               a value couldn't be turned into the requested type
│       ├── TypeMismatchError     column type incompatible with the Struct field annotation
│       ├── NullValueError        NULL in a column bound to a non-optional field
│       ├── MissingColumnError    required Struct field absent from the result
│       ├── UnknownColumnError    extra column with forbid_unknown_fields=True
│       └── UnsupportedTypeError  PostgreSQL type fastpg can't decode
└── DatabaseError                 error reported by the server (has .sqlstate etc.)
    ├── DataError (22)
    ├── IntegrityError (23)  → UniqueViolationError, ForeignKeyViolationError, ...
    ├── TransactionError (40) → SerializationError, DeadlockDetectedError
    ├── ProgrammingError (42) → SQLSyntaxError, UndefinedTableError, UndefinedColumnError
    ├── OperationalError (08, 53, 57) → QueryCanceledError
    ├── InternalError (XX)
    └── NotSupportedError (0A)
"""

from typing import ClassVar


class FastPGError(Exception):
    """Base class for all fastpg errors."""

    row_index: int | None = None
    """For bulk writes (`execute_many`, `insert_many`, `copy_in`): the 0-based index of the
    input row that failed, when known."""


Error = FastPGError
"""DB-API 2.0 (PEP 249) name for `FastPGError`, the base class of every fastpg error."""


class InterfaceError(FastPGError):
    """Client-side failure (connection state, protocol, I/O)."""


class ConnectionClosedError(InterfaceError):
    """The connection is closed or was lost."""


class ParameterError(InterfaceError):
    """A query parameter has no value (named parameters) or can't be encoded for its type."""


class ResultError(FastPGError):
    """The result does not match what the caller asked for."""


class NoRowsError(ResultError, LookupError):
    """`select_one()` returned no rows."""


class TooManyRowsError(ResultError):
    """A single-row select returned more than one row."""


class DecodeError(ResultError):
    """A value could not be converted to the requested Python type."""


class TypeMismatchError(DecodeError, TypeError):
    """A column's PostgreSQL type is incompatible with the target field's annotation."""


class NullValueError(DecodeError, ValueError):
    """A NULL was returned for a field that is not optional."""


class MissingColumnError(DecodeError):
    """A required Struct field has no corresponding column in the result."""


class UnknownColumnError(DecodeError):
    """The result has a column the Struct does not declare (forbid_unknown_fields=True)."""


class UnsupportedTypeError(DecodeError):
    """The PostgreSQL type (or field annotation) is not supported by the chosen strategy."""


class DatabaseError(FastPGError):
    """An error reported by the PostgreSQL server."""

    sqlstate: str
    severity: str
    detail: str | None
    hint: str | None
    position: int | None
    schema_name: str | None
    table_name: str | None
    column_name: str | None
    data_type_name: str | None
    constraint_name: str | None

    def __init__(
        self,
        message: str,
        *,
        sqlstate: str = "",
        severity: str = "ERROR",
        detail: str | None = None,
        hint: str | None = None,
        position: int | None = None,
        schema_name: str | None = None,
        table_name: str | None = None,
        column_name: str | None = None,
        data_type_name: str | None = None,
        constraint_name: str | None = None,
    ) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate
        self.severity = severity
        self.detail = detail
        self.hint = hint
        self.position = position
        self.schema_name = schema_name
        self.table_name = table_name
        self.column_name = column_name
        self.data_type_name = data_type_name
        self.constraint_name = constraint_name

    def __str__(self) -> str:
        message = super().__str__()
        parts = [f"{message} (SQLSTATE {self.sqlstate})" if self.sqlstate else message]
        if self.detail:
            parts.append(f"DETAIL: {self.detail}")
        if self.hint:
            parts.append(f"HINT: {self.hint}")
        return "\n".join(parts)

    @classmethod
    def from_server(
        cls,
        sqlstate: str,
        message: str,
        severity: str,
        detail: str | None,
        hint: str | None,
        position: int | None,
        schema_name: str | None,
        table_name: str | None,
        column_name: str | None,
        data_type_name: str | None,
        constraint_name: str | None,
    ) -> "DatabaseError":
        """Build the most specific subclass for a server ErrorResponse (called from Rust)."""
        return SQLStateRegistry.error_class(sqlstate)(
            message,
            sqlstate=sqlstate,
            severity=severity,
            detail=detail,
            hint=hint,
            position=position,
            schema_name=schema_name,
            table_name=table_name,
            column_name=column_name,
            data_type_name=data_type_name,
            constraint_name=constraint_name,
        )


class DataError(DatabaseError):
    """SQLSTATE class 22: data exception."""


class IntegrityError(DatabaseError):
    """SQLSTATE class 23: integrity constraint violation."""


class NotNullViolationError(IntegrityError):
    """23502."""


class ForeignKeyViolationError(IntegrityError):
    """23503."""


class UniqueViolationError(IntegrityError):
    """23505."""


class CheckViolationError(IntegrityError):
    """23514."""


class TransactionError(DatabaseError):
    """SQLSTATE class 40: transaction rollback."""


class SerializationError(TransactionError):
    """40001."""


class DeadlockDetectedError(TransactionError):
    """40P01."""


class ProgrammingError(DatabaseError):
    """SQLSTATE class 42: syntax error or access rule violation."""


class SQLSyntaxError(ProgrammingError):
    """42601."""


class UndefinedColumnError(ProgrammingError):
    """42703."""


class UndefinedTableError(ProgrammingError):
    """42P01."""


class OperationalError(DatabaseError):
    """SQLSTATE classes 08, 53, 57: connection, resources, operator intervention."""


class QueryCanceledError(OperationalError):
    """57014 (includes statement_timeout)."""


class InternalError(DatabaseError):
    """SQLSTATE class XX: internal error."""


class NotSupportedError(DatabaseError):
    """SQLSTATE class 0A: feature not supported by the server."""


class SQLStateRegistry:
    """Which DatabaseError subclass represents a SQLSTATE code."""

    BY_CODE: ClassVar[dict[str, type[DatabaseError]]] = {
        "23502": NotNullViolationError,
        "23503": ForeignKeyViolationError,
        "23505": UniqueViolationError,
        "23514": CheckViolationError,
        "40001": SerializationError,
        "40P01": DeadlockDetectedError,
        "42601": SQLSyntaxError,
        "42703": UndefinedColumnError,
        "42P01": UndefinedTableError,
        "57014": QueryCanceledError,
    }
    BY_CLASS: ClassVar[dict[str, type[DatabaseError]]] = {
        "08": OperationalError,
        "0A": NotSupportedError,
        "22": DataError,
        "23": IntegrityError,
        "40": TransactionError,
        "42": ProgrammingError,
        "53": OperationalError,
        "57": OperationalError,
        "XX": InternalError,
    }

    @classmethod
    def error_class(cls, sqlstate: str) -> type[DatabaseError]:
        """The most specific class for `sqlstate` (most codes have no dedicated subclass)."""
        specific = cls.BY_CODE.get(sqlstate)
        if specific is not None:
            return specific
        return cls.BY_CLASS.get(sqlstate[:2], DatabaseError)
