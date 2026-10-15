"""fastpg: async PostgreSQL client for Python, implemented in Rust.

```python
async with await fastpg.connect(dsn) as conn:
    person = await conn.select_one(
        "SELECT name, age FROM people WHERE id = $1", 42, into=Person
    )
    # or, as a t-string (interpolations become bound parameters, never SQL text):
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
from fastpg._named import NamedParams
from fastpg._native import __version__
from fastpg._pool import Pool, PoolStats, create_pool
from fastpg._template import Identifier
from fastpg.errors import (
    ConnectionClosedError,
    DatabaseError,
    DataError,
    DecodeError,
    Error,
    FastPGError,
    IntegrityError,
    InterfaceError,
    InternalError,
    MissingColumnError,
    NoRowsError,
    NotSupportedError,
    NullValueError,
    OperationalError,
    ParameterError,
    ProgrammingError,
    ResultError,
    TooManyRowsError,
    TypeMismatchError,
    UniqueViolationError,
    UnknownColumnError,
    UnsupportedTypeError,
)

__all__ = [
    "Connection",
    "ConnectionClosedError",
    "DataError",
    "DatabaseError",
    "DecodeError",
    "Error",
    "FastPGError",
    "Identifier",
    "IntegrityError",
    "InterfaceError",
    "InternalError",
    "IsolationLevel",
    "MissingColumnError",
    "NamedParams",
    "NoRowsError",
    "NotSupportedError",
    "NullValueError",
    "OperationalError",
    "ParameterError",
    "Pool",
    "PoolStats",
    "ProgrammingError",
    "ResultError",
    "Strategy",
    "TooManyRowsError",
    "Transaction",
    "TypeMismatchError",
    "UniqueViolationError",
    "UnknownColumnError",
    "UnsupportedTypeError",
    "__version__",
    "connect",
    "create_pool",
]
