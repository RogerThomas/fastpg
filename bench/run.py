#!yeet
"""Benchmark Python PostgreSQL drivers against the docker-compose database.

    uv run --directory <repo> --no-sync yeet ./bench/run.py [options]

Each driver adapter lives in ``bench/drivers/<name>.py``; see ``drivers/base.py`` for the
protocol. Results are printed and written to ``bench/results/<timestamp>.{json,md}``.
"""

import dataclasses
import datetime as dt
import fnmatch
import gc
import logging
import platform
from importlib import metadata
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit, urlunsplit

import report
from drivers import Registry, discover
from drivers.base import DEFAULT_DSN, AsyncSpec, DriverSpec, kind_of
from harness import (
    ALLOC_CASES,
    LOOP,
    SEQUENTIAL,
    Plan,
    Result,
    Scenario,
    run_async_driver,
    run_coroutine,
    run_sync_driver,
)
from yeetr import Opt

logger = logging.getLogger("bench")

RESULTS_DIR = Path(__file__).resolve().parent / "results"


def _is_sync(spec: DriverSpec) -> bool:
    return kind_of(spec) == "sync"


def _select(registry: Registry, patterns: list[str]) -> list[DriverSpec]:
    """Specs matching any glob in ``patterns`` (all if empty), async drivers first."""
    names = list(registry.specs)
    if patterns:
        chosen: set[str] = set()
        for pattern in patterns:
            matches = fnmatch.filter(names, pattern)
            if not matches:
                unavailable = ", ".join(registry.unavailable) or "none"
                msg = (
                    f"no driver matches {pattern!r}. Available: {', '.join(names)}. "
                    f"Modules that failed to import: {unavailable} (see --list-drivers)."
                )
                raise SystemExit(msg)
            chosen.update(matches)
        names = [n for n in names if n in chosen]
    else:
        for module, reason in registry.unavailable.items():
            logger.warning("skipping drivers/%s.py: %s", module, reason)
    specs = [registry.specs[n] for n in names]
    return sorted(specs, key=_is_sync)  # stable: keeps module order


def _planned(spec: DriverSpec, plan: Plan) -> list[str]:
    """Result scenario names this driver is expected to produce."""
    names: list[str] = [s for s in SEQUENTIAL if s in plan.scenarios]
    if isinstance(spec, AsyncSpec):
        names += [
            s for s in (Scenario.CONCURRENT_PINNED, Scenario.CONCURRENT) if s in plan.scenarios
        ]
    if Scenario.ALLOC in plan.scenarios:
        names += [name for name, _ in ALLOC_CASES]
    return names


def _run_driver(spec: DriverSpec, plan: Plan, results: list[Result]) -> None:
    """Run one driver; on failure, log it and record an error row for each missing scenario."""
    gc.collect()
    first = len(results)
    try:
        if isinstance(spec, AsyncSpec):
            run_coroutine(run_async_driver(spec, plan, results))
        else:
            run_sync_driver(spec, plan, results)
    except Exception as exc:
        logger.exception("%s failed", spec.name)
        done = {r.scenario for r in results[first:]}
        results.extend(
            Result(
                driver=spec.name,
                kind=kind_of(spec),
                rows=spec.rows,
                scenario=scenario,
                error=f"{type(exc).__name__}: {exc}",
            )
            for scenario in _planned(spec, plan)
            if scenario not in done
        )


def _redact(dsn: str) -> str:
    url = urlsplit(dsn)
    if url.password is None:
        return dsn
    netloc = url.netloc.replace(f":{url.password}@", ":***@", 1)
    return urlunsplit(url._replace(netloc=netloc))


def _versions(specs: list[DriverSpec]) -> dict[str, str]:
    packages = {spec.package for spec in specs} | {"msgspec"}
    if LOOP == "uvloop":
        packages.add("uvloop")
    versions: dict[str, str] = {}
    for package in sorted(packages):
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            versions[package] = "unknown"
    return versions


def main(
    *,
    drivers: Annotated[
        list[str] | None,
        Opt(alias="d", help="Name/glob, repeatable: -d asyncpg -d '*-struct'. Default: all"),
    ] = None,
    scenarios: Annotated[
        list[Scenario] | None,
        Opt(alias="s", help="Scenario to run, repeatable. Default: all"),
    ] = None,
    duration: Annotated[
        float,
        Opt(alias="t", help="Measured seconds per scenario per driver"),
    ] = 5.0,
    iterations: Annotated[
        int | None,
        Opt(alias="n", help="Measured ops per scenario per driver; overrides --duration"),
    ] = None,
    warmup: Annotated[
        float,
        Opt(alias="w", help="Warmup seconds per scenario (always at least 10 ops)"),
    ] = 1.0,
    concurrency: Annotated[
        int,
        Opt(alias="c", help="Tasks, and pool size or connections, for the concurrent scenarios"),
    ] = 16,
    pool_size: Annotated[
        int | None,
        Opt(help="Pool size (min = max) for the concurrent scenario. Default: --concurrency"),
    ] = None,
    alloc_iterations: Annotated[
        int,
        Opt(help="Ops traced per allocation case (separate, untimed pass)"),
    ] = 200,
    seed: Annotated[int, Opt(help="Seed for the deterministic id streams")] = 42,
    dsn: Annotated[str, Opt(envvar="FASTPG_DSN", help="Database URL")] = DEFAULT_DSN,
    output_dir: Annotated[
        Path,
        Opt(alias="o", help="Where to write <timestamp>.json and .md"),
    ] = RESULTS_DIR,
    list_drivers: Annotated[
        bool,
        Opt(alias="l", help="List discovered drivers and exit"),
    ] = False,
) -> None:
    """Benchmark the registered PostgreSQL drivers."""
    registry = discover()
    if list_drivers:
        report.print_registry(registry)
        return
    if iterations is not None and iterations < 2:
        msg = "--iterations must be at least 2"
        raise SystemExit(msg)
    if pool_size is None:
        pool_size = concurrency
    if concurrency < 1 or pool_size < 1 or alloc_iterations < 1:
        msg = "--concurrency, --pool-size and --alloc-iterations must be positive"
        raise SystemExit(msg)
    specs = _select(registry, drivers or [])
    plan = Plan(
        dsn=dsn,
        scenarios=tuple(scenarios) if scenarios else tuple(Scenario),
        duration_s=duration,
        iterations=iterations,
        warmup_s=warmup,
        concurrency=concurrency,
        pool_size=pool_size,
        alloc_iterations=alloc_iterations,
        seed=seed,
    )
    started_at = dt.datetime.now(dt.UTC)
    logger.info(
        "%d drivers x %s on %s (event loop: %s)",
        len(specs),
        ", ".join(plan.scenarios),
        _redact(dsn),
        LOOP,
    )
    results: list[Result] = []
    for spec in specs:
        _run_driver(spec, plan, results)

    run = report.RunReport(
        started_at=started_at,
        python=platform.python_version(),
        platform=platform.platform(terse=True),
        event_loop=LOOP,
        plan=dataclasses.replace(plan, dsn=_redact(dsn)),
        versions=_versions(specs),
        results=results,
    )
    report.print_tables(run)
    json_path, md_path = report.write(run, output_dir)
    logger.info("wrote %s and %s", json_path, md_path)
