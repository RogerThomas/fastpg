# Errors

Every error fastpg raises is a `fastpg.FastPGError`.
Errors from the server get a class that says what went wrong:

```python
await conn.execute_script("CREATE TEMP TABLE members (email text CONSTRAINT members_key UNIQUE)")
await conn.execute("INSERT INTO members VALUES ($1)", "a@example.com")

try:
    await conn.execute("INSERT INTO members VALUES ($1)", "a@example.com")
except fastpg.UniqueViolationError as e:
    print(e.constraint_name)  # members_key
    print(e.detail)  # Key (email)=(a@example.com) already exists.
```

The connection stays usable after a server error.

## The error classes

```
FastPGError
├── InterfaceError                a problem on the client side
│   └── ParameterError            a value can't be sent as its column's type
├── ResultError
│   ├── NoRowsError               select_one() found no row
│   ├── TooManyRowsError          a single-row select found more than one
│   └── DecodeError               a value can't become the type you asked for
│       ├── TypeMismatchError     a column's type doesn't fit its field
│       ├── NullValueError        NULL in a field that isn't optional
│       ├── MissingColumnError    a required field has no column
│       ├── UnknownColumnError    an extra column, when the type forbids them
│       └── UnsupportedTypeError  a type or strategy fastpg can't use
└── DatabaseError                 an error from the server, or reaching it
    ├── DataError                 bad data, e.g. a division by zero
    ├── IntegrityError            a constraint failed
    │   └── NotNullViolationError, ForeignKeyViolationError,
    │       UniqueViolationError, CheckViolationError
    ├── ProgrammingError          bad SQL, e.g. a missing table
    │   └── SQLSyntaxError, UndefinedTableError, UndefinedColumnError
    ├── OperationalError          the database can't be reached or used
    │   ├── TransactionError      the server rolled the transaction back
    │   │   └── SerializationError, DeadlockDetectedError
    │   ├── ConnectError          connecting failed: refused, unreachable, timed out
    │   ├── ConnectionClosedError the connection is closed or was lost
    │   ├── AuthenticationError   e.g. a wrong password
    │   └── QueryCanceledError    cancelled, e.g. by statement_timeout
    ├── InternalError             an internal server error
    └── NotSupportedError         a feature the server doesn't support
```

All of them can be imported from `fastpg`.

## Which server errors raise which class

A server error's class comes from the first two characters of its SQLSTATE,
the same way psycopg 3 does it:

* `DataError`: 22
* `IntegrityError`: 23
* `TransactionError`: 40
* `ProgrammingError`: 10, 20, 21, 26, 34, 3D, 3F, 42, 44, P0
* `OperationalError`: 08, 27, 2F, 38, 39, 3B, 53, 54, 55, 57, 58, F0, HV
* `AuthenticationError`: 28
* `InternalError`: 24, 25, 2B, 2D, XX
* `NotSupportedError`: 0A
* `DatabaseError`: any other class

For example, a missing database (3D000) and `RAISE EXCEPTION` (P0001) raise
`ProgrammingError`, a lock you can't get (55P03) raises `OperationalError`, and a query
in a transaction an earlier error aborted (25P02) raises `InternalError`.
Catching `DatabaseError` still catches them all.

To handle "the database is unreachable", catch `OperationalError`.

## What a server error tells you

A `DatabaseError` has the server's details: `sqlstate`, `detail`, `hint`, `table_name`,
`column_name`, `constraint_name` and more.
Each is `None` when the server doesn't send it.
For `ConnectError` and `ConnectionClosedError` they are all `None`.

## Good to know

* If the connection is lost, queries on it fail with `ConnectionClosedError`.
  fastpg doesn't retry them, because a query may already have run.
* To limit how long queries can run, put `statement_timeout` in the DSN, e.g.
  `?options=-c%20statement_timeout%3D5000` (milliseconds).
  A query that runs too long raises `QueryCanceledError`.
* A mistake in the call itself, such as a wrong `into=` type, raises `TypeError` or
  `ValueError`.
