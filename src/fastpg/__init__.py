"""fastpg: async PostgreSQL client for Python, implemented in Rust.

```python
async with await fastpg.connect(dsn) as conn:
    person = await conn.select_one(
        "SELECT name, age FROM people WHERE id = $1", 42, into=Person
    )
    # or, on Python 3.14+, as a t-string (interpolations become bound parameters, never SQL text):
    person = await conn.select_one(
        t"SELECT name, age FROM people WHERE id = {person_id}", into=Person
    )
```
"""

from fastpg._connection import (
    Connection,
    IsolationLevel,
    Strategy,
    Transaction,
    connect,
)
from fastpg._listen import Listener, Notification
from fastpg._named import NamedParams
from fastpg._native import __version__
from fastpg._pool import Pool, PoolStats, create_pool
from fastpg._template import Identifier
from fastpg.errors import (
    AuthenticationError,
    CheckViolationError,
    ConnectError,
    ConnectionClosedError,
    DatabaseError,
    DataError,
    DeadlockDetectedError,
    DecodeError,
    Error,
    FastPGError,
    ForeignKeyViolationError,
    IntegrityError,
    InterfaceError,
    InternalError,
    ListenerOverflowError,
    MissingColumnError,
    NoRowsError,
    NotNullViolationError,
    NotSupportedError,
    NullValueError,
    OperationalError,
    ParameterError,
    PoolTimeoutError,
    ProgrammingError,
    QueryCanceledError,
    ResultError,
    SerializationError,
    SQLSyntaxError,
    TooManyRowsError,
    TransactionError,
    TypeMismatchError,
    UndefinedColumnError,
    UndefinedTableError,
    UniqueViolationError,
    UnknownColumnError,
    UnsupportedTypeError,
)

__all__ = [
    "AuthenticationError",
    "CheckViolationError",
    "ConnectError",
    "Connection",
    "ConnectionClosedError",
    "DataError",
    "DatabaseError",
    "DeadlockDetectedError",
    "DecodeError",
    "Error",
    "FastPGError",
    "ForeignKeyViolationError",
    "Identifier",
    "IntegrityError",
    "InterfaceError",
    "InternalError",
    "IsolationLevel",
    "Listener",
    "ListenerOverflowError",
    "MissingColumnError",
    "NamedParams",
    "NoRowsError",
    "NotNullViolationError",
    "NotSupportedError",
    "Notification",
    "NullValueError",
    "OperationalError",
    "ParameterError",
    "Pool",
    "PoolStats",
    "PoolTimeoutError",
    "ProgrammingError",
    "QueryCanceledError",
    "ResultError",
    "SQLSyntaxError",
    "SerializationError",
    "Strategy",
    "TooManyRowsError",
    "Transaction",
    "TransactionError",
    "TypeMismatchError",
    "UndefinedColumnError",
    "UndefinedTableError",
    "UniqueViolationError",
    "UnknownColumnError",
    "UnsupportedTypeError",
    "__version__",
    "connect",
    "create_pool",
]
