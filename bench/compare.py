#!yeet
"""Defensible cross-driver comparison: fastpg vs asyncpg, psycopg 3 (async) and psqlpy.

Creates and seeds its own ``fastpg_bench`` schema, checks every driver returns (and writes)
exactly the expected data, then runs every workload for every driver configuration over
``--trials`` trials, with the configurations' order shuffled per trial. Writes
``compare-<timestamp>.json`` (environment, settings, every run, aggregates) and ``.md``
into ``--output-dir``; ``--summary`` also appends the Markdown to a file such as
``$GITHUB_STEP_SUMMARY``. Exits non-zero if any run crashed or returned wrong data, never
because of the numbers. Methodology: ``docs/benchmarks.md``.

    uv run yeet ./bench/compare.py                          # the CI defaults
    uv run yeet ./bench/compare.py -n 5 -t 10               # longer, steadier local run
    uv run yeet ./bench/compare.py -k 'point-*' -d fastpg -d 'asyncpg*'
    uv run yeet ./bench/compare.py --list
"""

import fnmatch
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

from comparison import report
from comparison.adapters import Config, Configs
from comparison.runner import Plan, applies, run_all
from comparison.workloads import Workloads
from drivers.base import DEFAULT_DSN
from harness import LOOP, run_coroutine
from yeetr import Opt

if TYPE_CHECKING:
    from comparison.runner import Run
    from comparison.workloads import Workload

logger = logging.getLogger("bench")


def _select[T: (Workload, Config)](
    items: list[T],
    patterns: list[str] | None,
    what: str,
) -> list[T]:
    if not patterns:
        return items
    for pattern in patterns:
        if not any(fnmatch.fnmatch(item.name, pattern) for item in items):
            names = ", ".join(item.name for item in items)
            msg = f"no {what} matches {pattern!r}; available: {names}"
            raise SystemExit(msg)
    return [i for i in items if any(fnmatch.fnmatch(i.name, p) for p in patterns)]


def _skipped(workloads: list[Workload], configs: list[Config]) -> dict[str, dict[str, str]]:
    reason = "same code path as `{}` (the connection is checked out exclusively)"
    return {
        w.name: {
            c.name: reason.format(c.shared_variant_of)
            for c in configs
            if not applies(w, c) and c.shared_variant_of is not None
        }
        for w in workloads
    }


def _list(workloads: list[Workload], configs: list[Config]) -> None:
    for w in workloads:
        logger.info("workload %-22s %s", w.name, w.title)
    for c in configs:
        logger.info("config   %-22s %s", c.name, c.notes)


async def _main(
    plan: Plan,
    workloads: list[Workload],
    configs: list[Config],
) -> tuple[report.Environment, list[Run]]:
    environment = await report.Probe.environment(plan.dsn, LOOP)
    runs = await run_all(plan, workloads, configs)
    return environment, runs


def main(
    *,
    dsn: Annotated[str, Opt(envvar="FASTPG_DSN", help="Database URL")] = DEFAULT_DSN,
    trials: Annotated[int, Opt(alias="n", help="Trials (each runs every workload once)")] = 3,
    duration: Annotated[float, Opt(alias="t", help="Measured seconds per run")] = 3.0,
    warmup: Annotated[float, Opt(alias="w", help="Warm-up seconds per run")] = 1.0,
    pool_size: Annotated[int, Opt(alias="p", help="Connections per pool, every driver")] = 16,
    rates: Annotated[
        list[int] | None,
        Opt(alias="r", help="Open-loop request rates per second, repeatable. Default: 1000, 4000"),
    ] = None,
    workloads: Annotated[
        list[str] | None,
        Opt(alias="k", help="Workload name/glob, repeatable. Default: all"),
    ] = None,
    drivers: Annotated[
        list[str] | None,
        Opt(alias="d", help="Configuration name/glob, repeatable. Default: all"),
    ] = None,
    seed: Annotated[int, Opt(help="Seed for argument streams, arrivals and run order")] = 42,
    max_inflight: Annotated[
        int,
        Opt(help="Open loop: outstanding requests at which a rate counts as overloaded"),
    ] = 2_000,
    output_dir: Annotated[
        Path | None,
        Opt(alias="o", help="Where to write the JSON and Markdown. Default: bench/results"),
    ] = None,
    summary: Annotated[
        Path | None,
        Opt(help="Also append the Markdown here, e.g. $GITHUB_STEP_SUMMARY"),
    ] = None,
    keep_schema: Annotated[
        bool,
        Opt(help="Reuse an already seeded fastpg_bench schema instead of recreating it"),
    ] = False,
    list_only: Annotated[bool, Opt("--list", help="List workloads and configurations")] = False,
) -> None:
    """Compare fastpg, asyncpg, psycopg and psqlpy on the same workloads."""
    chosen_workloads = _select(Workloads.all(rates or [1_000, 4_000]), workloads, "workload")
    configs = _select(list(Configs.all()), drivers, "configuration")
    if list_only:
        _list(chosen_workloads, configs)
        return
    if min(trials, pool_size) < 1 or duration <= 0 or warmup < 0:
        msg = "--trials and --pool-size must be positive, --duration > 0, --warmup >= 0"
        raise SystemExit(msg)
    plan = Plan(
        dsn=dsn,
        trials=trials,
        duration_s=duration,
        warmup_s=warmup,
        pool_size=pool_size,
        seed=seed,
        max_inflight=max_inflight,
        keep_schema=keep_schema,
    )
    per_trial = sum(1 for w in chosen_workloads for c in configs if applies(w, c))
    logger.info(
        "%d runs per trial x %d trials, roughly %.0f min (event loop: %s, DSN: %s)",
        per_trial,
        trials,
        per_trial * trials * (warmup + duration + 0.5) / 60,
        LOOP,
        report.redact(dsn),
    )
    environment, runs = run_coroutine(_main(plan, chosen_workloads, configs))
    skipped = _skipped(chosen_workloads, configs)
    result = report.build(environment, plan, chosen_workloads, configs, runs, skipped)
    json_path, md_path = report.write(result, output_dir or Path(__file__).parent / "results")
    logger.info("wrote %s and %s", json_path, md_path)
    if summary is not None:
        with summary.open("a") as out:
            out.write(md_path.read_text())
    if result.failures:
        msg = f"{len(result.failures)} runs crashed or returned wrong data (see {md_path})"
        raise SystemExit(msg)
