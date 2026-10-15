# 0009. Errors

## Context

Callers need to catch errors by kind: client misuse, an unexpected result shape, values
that cannot be decoded, and server errors by SQLSTATE. Code written against other Python
drivers expects PEP 249 names.

## Decision

- The hierarchy is defined once, in Python (`src/fastpg/errors.py`); the Rust extension
  raises those classes by name.
- `FastPGError` (alias `Error`) has three branches: `InterfaceError` (including
  `ConnectionClosedError`, also when the server drops the connection, and
  `ParameterError`), `ResultError` (`NoRowsError`, which is also a `LookupError`,
  `TooManyRowsError` and the `DecodeError` family), and `DatabaseError`, which carries
  `sqlstate`, `detail`, `hint` and constraint and table names.
- A server error maps to the most specific subclass: by code where one exists (for example
  `UniqueViolationError` for 23505, `SerializationError` for 40001), otherwise by class:
  22 `DataError`, 23 `IntegrityError`, 40 `TransactionError`, 42 `ProgrammingError`,
  08/53/57 `OperationalError`, XX `InternalError`, 0A `NotSupportedError`.
- Every PEP 249 name except `Warning` is exported, in PEP 249's place. fastpg raises no
  warnings, and the name would shadow the builtin.
- `select_one` raises on zero or several rows; `select_one_or_none` raises on several.

## Consequences

Handlers can use fastpg's specific classes or the generic DB-API names. A server error
leaves the connection usable. An unexpected row count is always an error rather than a
silently dropped row.
