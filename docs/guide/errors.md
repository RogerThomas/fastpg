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
│   ├── ConnectionClosedError     the connection is closed or was lost
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
└── DatabaseError                 an error from the server
    ├── DataError                 bad data, e.g. a division by zero
    ├── IntegrityError            a constraint failed
    │   └── NotNullViolationError, ForeignKeyViolationError,
    │       UniqueViolationError, CheckViolationError
    ├── TransactionError          the server rolled the transaction back
    │   └── SerializationError, DeadlockDetectedError
    ├── ProgrammingError          bad SQL, e.g. a missing table
    │   └── SQLSyntaxError, UndefinedTableError, UndefinedColumnError
    ├── OperationalError          e.g. the server is out of resources
    │   └── QueryCanceledError    cancelled, e.g. by statement_timeout
    ├── InternalError             an internal server error
    └── NotSupportedError         a feature the server doesn't support
```

All of them are in `fastpg.errors`.
The common ones can also be imported from `fastpg`.

## What a server error tells you

A `DatabaseError` has the server's details: `sqlstate`, `detail`, `hint`, `table_name`,
`column_name`, `constraint_name` and more.
Each is `None` when the server doesn't send it.

## Good to know

* If the connection is lost, queries on it fail with `ConnectionClosedError`.
  fastpg doesn't retry them, because a query may already have run.
* To limit how long queries can run, put `statement_timeout` in the DSN, e.g.
  `?options=-c%20statement_timeout%3D5000` (milliseconds).
  A query that runs too long raises `QueryCanceledError`.
* A mistake in the call itself, such as a wrong `into=` type, raises `TypeError` or
  `ValueError`.
