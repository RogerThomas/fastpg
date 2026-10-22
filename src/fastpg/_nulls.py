"""A NULL in a field that isn't optional, as the msgpack and json routes come to see it.

The direct route raises `NullValueError` while it decodes a row. Through msgpack or JSON, a
NULL reaches the validating decoder (msgspec's or pydantic's) as `nil`/`null`, and its error
would otherwise become a plain `DecodeError`: `null_value_error` recognises that case, so every
route raises the same error, with the same message. Only a failed decode pays for it.
"""

import re
from typing import Any, cast

import msgspec

from fastpg.errors import NullValueError

# msgspec: "Expected `int`, got `null` - at `$.value`" (JSON; msgpack says `None`), with
# `$[3].value` for a row of `select_many`. Only a top-level field: a column.
_MSGSPEC_NULL = re.compile(r"got `(?:null|None)` - at `\$(?:\[(\d+)\])?\.([^`.\[]+)`$")


def _pydantic_column(error: Exception) -> tuple[int | None, str] | None:
    """The row and column of a pydantic "wrong type" error whose input was None, if any."""
    errors = getattr(error, "errors", None)
    if not callable(errors):
        return None
    details = cast("list[dict[str, Any]]", errors())
    for detail in details:
        loc = cast("tuple[object, ...]", detail.get("loc", ()))
        # A type error (int_type, string_type, ...), not a validator's own: those may refuse
        # None for reasons of their own, and keep their message.
        if detail.get("input", ...) is not None or not str(detail.get("type")).endswith("_type"):
            continue
        match loc:
            case (str(column),):
                return None, column
            case (int(row), str(column)):
                return row, column
            case _:
                continue
    return None


def _attribute(into: type[Any], column: str) -> str:
    """The field of `into` that `column` fills (its name may differ: renames, aliases)."""
    if issubclass(into, msgspec.Struct):
        for info in msgspec.structs.fields(into):
            if info.encode_name == column:
                return info.name
    model_fields: object = getattr(into, "model_fields", None)
    if isinstance(model_fields, dict):
        for name, info in model_fields.items():  # pyright: ignore[reportUnknownVariableType]
            aliases = (getattr(info, "alias", None), getattr(info, "validation_alias", None))  # pyright: ignore[reportUnknownArgumentType]
            if column == name or column in aliases:
                return str(name)  # pyright: ignore[reportUnknownArgumentType]
    return column


def null_value_error(error: Exception, into: type[Any]) -> NullValueError | None:
    """`NullValueError` for a validating decoder's `error`, if it's a NULL in a field that
    isn't optional; None for any other error."""
    if isinstance(error, msgspec.ValidationError):
        match = _MSGSPEC_NULL.search(str(error))
        found = (None if match[1] is None else int(match[1]), match[2]) if match else None
    else:
        found = _pydantic_column(error)
    if found is None:
        return None
    row, column = found
    message = f'column "{column}" is NULL but {into.__name__}.{_attribute(into, column)} is not optional'
    # select_many: the row (0-based), which the direct route doesn't know.
    return NullValueError(message if row is None else f"{message} (row {row})")
