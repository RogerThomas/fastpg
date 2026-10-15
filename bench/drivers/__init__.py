"""Driver registry.

Every public module in this package (anything not starting with ``_`` and not ``base``)
is imported lazily and must expose ``DRIVERS: tuple[DriverSpec, ...]``. To add a driver,
drop a new module in here; nothing else needs registering. A module whose import fails
(e.g. the client library isn't built yet) is reported as unavailable instead of breaking
the whole suite.
"""

import importlib
import pkgutil
from dataclasses import dataclass, field
from operator import attrgetter
from typing import TypeIs

from .base import AsyncSpec, DriverSpec, SyncSpec

_SKIP = frozenset({"base"})


@dataclass(slots=True)
class Registry:
    """Discovered adapters, keyed by name, plus modules that failed to import."""

    specs: dict[str, DriverSpec] = field(default_factory=dict[str, DriverSpec])
    unavailable: dict[str, str] = field(default_factory=dict[str, str])


class DriverModuleError(TypeError):
    """A driver module doesn't expose a valid ``DRIVERS`` tuple."""


def _is_tuple(obj: object) -> TypeIs[tuple[object, ...]]:
    return isinstance(obj, tuple)


def _specs_of(module_name: str) -> tuple[DriverSpec, ...]:
    module = importlib.import_module(f"{__name__}.{module_name}")
    drivers: object = getattr(module, "DRIVERS", None)
    if not _is_tuple(drivers):
        msg = f"{module.__name__} must define DRIVERS: tuple[DriverSpec, ...]"
        raise DriverModuleError(msg)
    specs: list[DriverSpec] = []
    for spec in drivers:
        if not isinstance(spec, AsyncSpec | SyncSpec):
            msg = f"{module.__name__}.DRIVERS contains {spec!r}, not an AsyncSpec/SyncSpec"
            raise DriverModuleError(msg)
        specs.append(spec)
    return tuple(specs)


def discover() -> Registry:
    """Import every driver module and collect its specs."""
    registry = Registry()
    for info in sorted(pkgutil.iter_modules(__path__), key=attrgetter("name")):
        if info.name.startswith("_") or info.name in _SKIP:
            continue
        try:
            specs = _specs_of(info.name)
        except ImportError as exc:
            registry.unavailable[info.name] = f"{type(exc).__name__}: {exc}"
            continue
        for spec in specs:
            if spec.name in registry.specs:
                msg = f"duplicate driver name {spec.name!r} in {info.name}"
                raise DriverModuleError(msg)
            registry.specs[spec.name] = spec
    return registry
