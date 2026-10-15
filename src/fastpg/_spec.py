"""Row-target planning: what the native decoder needs for a `msgspec.Struct` or pydantic model.

Each target type is inspected once (cached) and yields a `Plan`:

* `spec`: the native spec for **direct** construction, or None when the type can't be built
  directly (with the reason in `direct_error`).
* decoders for the **msgpack** route (msgspec only) and the **json** route (both), where
  Rust encodes rows and a native validating parser (msgspec / pydantic-core) builds the
  objects.

For msgspec Structs a field is either a plain scalar (optionally `| None`) that Rust builds
directly, a JSON-shaped type (list, dict, nested Struct, ...) decoded from a json/jsonb
column with a cached `msgspec.json.Decoder(<annotation>)`, or something only msgspec
validation produces correctly (`Meta` constraints, enums, literals, non-optional unions,
tz-constrained datetimes). Such Structs can't be built directly; strategy "auto" routes them
through msgpack instead. Pydantic models are analysed in `fastpg._pydantic`.
"""

import inspect
import sys
from dataclasses import dataclass
from enum import IntEnum
from typing import TYPE_CHECKING, Any, ClassVar, Literal

import msgspec
import msgspec.inspect as mi

from fastpg._native import StructSpec

if TYPE_CHECKING:
    from collections.abc import Callable


class Kind(IntEnum):
    """Field kinds understood by the native decoder (order must match Rust `FieldKind`)."""

    ANY = 0
    BOOL = 1
    INT = 2
    FLOAT = 3
    STR = 4
    BYTES = 5
    DATETIME = 6
    DATE = 7
    TIME = 8
    UUID = 9
    DECIMAL = 10
    JSON = 11


type FieldArg = tuple[str, str, int, bool, bool, object | None]
"""`(attr_name, encode_name, kind, nullable, required, json_decoder)` for `StructSpec`."""


class NeedsValidationError(Exception):
    """A field can only be produced correctly by the library's own validation."""


@dataclass(frozen=True, slots=True)
class Plan[T]:
    """Everything needed to produce `T` from rows, by every strategy."""

    spec: StructSpec | None
    """Native spec for direct construction, or None (see `direct_error`)."""
    direct_error: str | None
    fallback: Literal["msgpack", "json"]
    """Strategy "auto" uses when direct construction is impossible."""
    msgpack_one: Callable[[bytes], T] | None
    msgpack_many: Callable[[bytes], list[T]] | None
    msgpack_error: str | None
    json_one: Callable[[bytes], T] | None
    json_many: Callable[[bytes], list[T]] | None
    json_error: str | None
    validation_error: type[Exception]
    """Raised by the validating decoders; re-raised as `fastpg.DecodeError`."""
    text_columns: tuple[str, ...]
    """Columns feeding `str` fields: the msgpack/json routes encode a json/jsonb value there
    as its text, which is what direct construction gives such a field (ADR 0016)."""


class StructPlanner:
    """Builds `Plan`s for msgspec Structs (see the module docstring)."""

    _SCALARS: ClassVar[dict[type[mi.Type], Kind]] = {
        mi.AnyType: Kind.ANY,
        mi.BoolType: Kind.BOOL,
        mi.IntType: Kind.INT,
        mi.FloatType: Kind.FLOAT,
        mi.StrType: Kind.STR,
        mi.BytesType: Kind.BYTES,
        mi.DateTimeType: Kind.DATETIME,
        mi.DateType: Kind.DATE,
        mi.TimeType: Kind.TIME,
        mi.UUIDType: Kind.UUID,
        mi.DecimalType: Kind.DECIMAL,
    }
    _JSON_SHAPED: ClassVar[tuple[type[mi.Type], ...]] = (
        mi.ListType,
        mi.TupleType,
        mi.VarTupleType,
        mi.SetType,
        mi.FrozenSetType,
        mi.DictType,
        mi.StructType,
        mi.TypedDictType,
        mi.DataclassType,
        mi.NamedTupleType,
    )

    @staticmethod
    def _is_str(t: mi.Type) -> bool:
        """Whether a field type is `str` or `str | None` (constraints don't matter)."""
        if isinstance(t, mi.UnionType):
            members = [m for m in t.types if not isinstance(m, mi.NoneType)]
            return len(members) == 1 and isinstance(members[0], mi.StrType)
        return isinstance(t, mi.StrType)

    @classmethod
    def _classify(cls, t: mi.Type) -> tuple[Kind, bool]:
        """Return `(kind, nullable)` for a msgspec field type, or raise NeedsValidationError."""
        nullable = False
        if isinstance(t, mi.UnionType):
            members = [m for m in t.types if not isinstance(m, mi.NoneType)]
            if len(members) != 1:
                msg = "unions other than `T | None` need msgspec validation"
                raise NeedsValidationError(msg)
            nullable = len(members) != len(t.types)
            t = members[0]
        kind = cls._SCALARS.get(type(t))  # absent: not a scalar
        if kind is not None:
            # For msgspec's scalar inspect types, every field is a constraint (ge, max_length,
            # pattern, tz, ...). Direct construction would silently skip them.
            constraints = {
                name: value
                for name in t.__struct_fields__
                if (value := getattr(t, name)) is not None
            }
            if constraints:
                msg = f"constraints {constraints} need msgspec validation"
                raise NeedsValidationError(msg)
            return kind, nullable
        if isinstance(t, cls._JSON_SHAPED):
            return Kind.JSON, nullable
        msg = f"{type(t).__name__} needs msgspec validation"
        raise NeedsValidationError(msg)

    @classmethod
    def plan(cls, struct: type[msgspec.Struct]) -> Plan[msgspec.Struct]:
        info = mi.type_info(struct)
        if not isinstance(info, mi.StructType):  # guaranteed for Struct types
            msg = f"msgspec does not describe {struct!r} as a Struct"
            raise TypeError(msg)
        annotations = {f.name: f.type for f in msgspec.structs.fields(struct)}
        attr_names = {name: name for name in struct.__struct_fields__}  # interned names
        fields: list[FieldArg] = []
        direct_error = None
        for field in info.fields:
            try:
                kind, nullable = cls._classify(field.type)
            except NeedsValidationError as e:
                direct_error = f"{struct.__qualname__}.{field.name}: {e}"
                break
            decoder: Any = None
            if kind is Kind.JSON:
                decoder = msgspec.json.Decoder(annotations[field.name]).decode
            attr = attr_names[field.name]
            fields.append((attr, field.encode_name, kind, nullable, field.required, decoder))
        # kw_only fields can't be passed positionally; msgspec only exposes this via the
        # signature.
        positional = sum(
            p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
            for p in inspect.signature(struct).parameters.values()
        )
        # msgspec leaves a Struct untracked by the cyclic GC when no field value can hold a
        # reference; the native builder does the same for rows of scalars, unless
        # `__post_init__` or a `__dict__` could add references it can't see.
        config = struct.__struct_config__
        untrackable = config.gc and not config.dict and not hasattr(struct, "__post_init__")
        spec = None
        if direct_error is None:
            spec = StructSpec(
                struct,
                fields,
                forbid_unknown_fields=info.forbid_unknown_fields,
                positional_fields=positional,
                untrackable=untrackable,
            )
        # Rows are encoded as maps/objects, which array_like Structs don't accept.
        map_error = None
        if info.array_like:
            map_error = f"{struct.__qualname__} is array_like; rows are encoded as maps"
        return Plan(
            spec=spec,
            direct_error=direct_error,
            fallback="msgpack",
            msgpack_one=msgspec.msgpack.Decoder(struct).decode,
            msgpack_many=msgspec.msgpack.Decoder(list[struct]).decode,
            msgpack_error=map_error,
            json_one=msgspec.json.Decoder(struct).decode,
            json_many=msgspec.json.Decoder(list[struct]).decode,
            json_error=map_error,
            validation_error=msgspec.ValidationError,
            text_columns=tuple(f.encode_name for f in info.fields if cls._is_str(f.type)),
        )


class Planner:
    """One cached `Plan` per `into=` type: msgspec Structs or pydantic models."""

    _plans: ClassVar[dict[type[object], Plan[Any]]] = {}

    @staticmethod
    def _build(target: type[object]) -> Plan[Any]:
        if issubclass(target, msgspec.Struct):
            return StructPlanner.plan(target)
        # Only consult pydantic if the application already imported it.
        if "pydantic" in sys.modules:
            from fastpg._pydantic import ModelPlanner

            if ModelPlanner.accepts(target):
                return ModelPlanner.plan(target)
        msg = f"into= must be a msgspec.Struct or pydantic.BaseModel subclass, got {target!r}"
        raise TypeError(msg)

    @classmethod
    def plan_for(cls, target: object) -> Plan[Any]:
        """The plan for `target`; raises TypeError for anything else (untyped callers)."""
        if not isinstance(target, type):
            msg = f"into= must be a msgspec.Struct or pydantic.BaseModel subclass, got {target!r}"
            raise TypeError(msg)
        known: type[object] = target
        plan = cls._plans.get(known)  # absent: first use of this type
        if plan is None:
            plan = cls._plans[known] = cls._build(known)
        return plan
