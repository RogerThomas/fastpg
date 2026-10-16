# Errors

Every error fastpg raises for a database operation is a `fastpg.FastPGError`.

```python
await conn.execute_script("CREATE TEMP TABLE members (email text CONSTRAINT members_key UNIQUE)")
await conn.execute("INSERT INTO members VALUES ($1)", "a@example.com")

try:
    await conn.execute("INSERT INTO members VALUES ($1)", "a@example.com")
except fastpg.UniqueViolationError as e:
    print(e.sqlstate, e.constraint_name)  # 23505 members_key
    print(e.detail)  # Key (email)=(a@example.com) already exists.
```

```
FastPGError                       also importable as Error (PEP 249)
├── InterfaceError                client-side problems
│   ├── ConnectionClosedError     the connection is closed or was lost
│   └── ParameterError            a parameter can't be encoded for its type
├── ResultError
│   ├── NoRowsError               select_one() found no row (also a LookupError)
│   ├── TooManyRowsError          a single-row select found more than one
│   └── DecodeError               a value can't become the requested type
│       ├── TypeMismatchError     column type doesn't fit the field (also a TypeError)
│       ├── NullValueError        NULL for a non-optional field (also a ValueError)
│       ├── MissingColumnError    a required field has no column
│       ├── UnknownColumnError    an extra column, when the type forbids them
│       └── UnsupportedTypeError  a type or strategy fastpg can't decode with
└── DatabaseError                 reported by the server
    ├── DataError                 SQLSTATE class 22
    ├── IntegrityError            class 23
    │   └── NotNullViolationError, ForeignKeyViolationError,
    │       UniqueViolationError, CheckViolationError
    ├── TransactionError          class 40
    │   └── SerializationError, DeadlockDetectedError
    ├── ProgrammingError          class 42
    │   └── SQLSyntaxError, UndefinedTableError, UndefinedColumnError
    ├── OperationalError          classes 08, 53, 57
    │   └── QueryCanceledError    57014, including statement_timeout
    ├── InternalError             class XX
    └── NotSupportedError         class 0A
```

All of them are in `fastpg.errors`; the common ones are also exported from `fastpg`. Mistakes
in the call itself, such as a bad `into=` type or template, raise `TypeError` or `ValueError`.

* A server error is the most specific class for its SQLSTATE, and carries `sqlstate`,
  `severity`, `detail`, `hint`, `position`, `schema_name`, `table_name`, `column_name`,
  `data_type_name` and `constraint_name` (`None` when the server doesn't say).
* It leaves the connection usable; inside a transaction, see
  [Transactions](transactions.md#gotchas).
* `ConnectionClosedError` fails queries in flight at once when the server goes away. Nothing
  is retried, because a statement may already have run.
* To cap every query on a connection, put `statement_timeout` in the DSN:
  `?options=-c%20statement_timeout%3D50` (milliseconds).
* A query cancelled by its caller (`asyncio.timeout(...)`, a cancelled task) is cancelled on
  the server too when it's the only one in flight on its connection, and the connection's
  next queries wait until the server has answered the cancel.
* Bulk-write errors carry `row_index` (see [Writes](writes.md#atomicity-and-errors)).
