"""Result rendering: rich tables on stdout, plus JSON and Markdown files under bench/results/."""

import datetime as dt
import sys
from typing import TYPE_CHECKING

import msgspec
from drivers.base import kind_of
from harness import ALLOC_CASES, Plan, Result, Scenario
from rich.console import Console
from rich.table import Table

if TYPE_CHECKING:
    from pathlib import Path

    from drivers import Registry

_TIMING_HEADERS = (
    *("driver", "kind", "rows", "ops/s", "rows/s"),
    *("p50 µs", "p90 µs", "p99 µs", "vs best"),
)
_ALLOC_HEADERS = ("driver", "kind", "rows", "peak B/op", "net B/op", "ops")
_ALLOC_SCENARIOS = frozenset(name for name, _ in ALLOC_CASES)
ALLOC_NOTE = (
    "tracemalloc only sees allocations made through Python's allocators: memory a C/Rust "
    "extension takes from malloc or its own allocator (libpq buffers, Rust heap) is invisible. "
    "peak = high-water mark during the op; net = bytes still held right after the op "
    "while the returned rows are alive (i.e. roughly the size of the result)."
)


class RunReport(msgspec.Struct, kw_only=True):
    started_at: dt.datetime
    python: str
    platform: str
    event_loop: str
    plan: Plan
    versions: dict[str, str]
    results: list[Result]


def _title(scenario: str, plan: Plan) -> str:
    c = plan.concurrency
    p = plan.pool_size
    return {
        Scenario.POINT: "Point lookup: 1 row by primary key, sequential on one connection",
        Scenario.RANGE_1K: "Range fetch: 1,000 rows, sequential on one connection",
        Scenario.RANGE_10K: "Large fetch: 10,000 rows, sequential on one connection",
        Scenario.CONCURRENT: (
            f"Concurrent point lookups: {c} tasks sharing the driver's pool of {p}"
        ),
        Scenario.CONCURRENT_PINNED: (
            f"Concurrent point lookups: {c} tasks, each pinned to its own connection"
        ),
        "alloc-point": "Allocations per op (tracemalloc): point lookup",
        "alloc-range-1k": "Allocations per op (tracemalloc): 1,000-row range",
    }.get(scenario, scenario)


def _num(value: float | None, fmt: str) -> str:
    return "-" if value is None else format(value, fmt)


def _peak_bytes(result: Result) -> float:
    return result.peak_bytes or 0.0


def _ops_per_s(result: Result) -> float:
    return result.ops_per_s or 0.0


def _rows_for(scenario: str, results: list[Result]) -> tuple[tuple[str, ...], list[list[str]]]:
    """Header and body cells for one scenario's table (shared by rich and Markdown)."""
    mine = [r for r in results if r.scenario == scenario]
    ok = [r for r in mine if r.error is None]
    failed = [r for r in mine if r.error is not None]
    body: list[list[str]]
    if scenario in _ALLOC_SCENARIOS:
        headers = _ALLOC_HEADERS
        ok.sort(key=_peak_bytes)
        body = [
            [
                *(r.driver, r.kind, r.rows),
                *(_num(r.peak_bytes, ",.0f"), _num(r.net_bytes, ",.0f"), f"{r.ops:,}"),
            ]
            for r in ok
        ]
    else:
        headers = _TIMING_HEADERS
        ok.sort(key=_ops_per_s, reverse=True)
        best = (ok[0].ops_per_s or 0.0) if ok else 0.0
        body = [
            [
                r.driver,
                r.kind,
                r.rows,
                _num(r.ops_per_s, ",.0f"),
                _num(r.rows_per_s, ",.0f"),
                _num(r.p50_us, ",.1f"),
                _num(r.p90_us, ",.1f"),
                _num(r.p99_us, ",.1f"),
                f"{(r.ops_per_s or 0.0) / best:.2f}x" if best else "-",
            ]
            for r in ok
        ]
    pad = [""] * (len(headers) - 4)
    body += [[r.driver, r.kind, r.rows, f"error: {r.error}", *pad] for r in failed]
    return headers, body


def _scenarios_in(run: RunReport) -> list[str]:
    seen: dict[str, None] = {}
    for r in run.results:
        seen.setdefault(r.scenario, None)
    return list(seen)


def _meta_line(run: RunReport) -> str:
    plan = run.plan
    budget = f"{plan.iterations} ops" if plan.iterations else f"{plan.duration_s:g}s"
    return (
        f"{run.started_at:%Y-%m-%d %H:%M:%S %Z} · Python {run.python} · {run.platform} · "
        f"loop={run.event_loop} · {budget} per scenario after {plan.warmup_s:g}s warmup · "
        f"seed={plan.seed}"
    )


def _console() -> Console:
    """Wide enough for the timing tables even when stdout isn't a terminal."""
    return Console() if sys.stdout.isatty() else Console(width=140)


def print_tables(run: RunReport, console: Console | None = None) -> None:
    console = console or _console()
    console.print(f"[dim]{_meta_line(run)}[/dim]")
    for scenario in _scenarios_in(run):
        headers, body = _rows_for(scenario, run.results)
        table = Table(title=_title(scenario, run.plan), title_justify="left")
        for i, header in enumerate(headers):
            table.add_column(header, justify="left" if i < 3 else "right", no_wrap=i == 0)
        for cells in body:
            table.add_row(*cells)
        console.print(table)
    if any(r.scenario in _ALLOC_SCENARIOS for r in run.results):
        console.print(f"[dim]{ALLOC_NOTE}[/dim]")


def to_markdown(run: RunReport) -> str:
    lines = ["# PostgreSQL driver benchmarks", "", _meta_line(run), ""]
    versions = ", ".join(f"{name} {version}" for name, version in sorted(run.versions.items()))
    lines += [f"Versions: {versions}", ""]
    for scenario in _scenarios_in(run):
        headers, body = _rows_for(scenario, run.results)
        lines += [f"## {_title(scenario, run.plan)}", ""]
        lines.append("| " + " | ".join(headers) + " |")
        lines.append("|" + "|".join("---" if i < 3 else "---:" for i in range(len(headers))) + "|")
        lines += ["| " + " | ".join(cells) + " |" for cells in body]
        lines.append("")
    if any(r.scenario in _ALLOC_SCENARIOS for r in run.results):
        lines += [f"> {ALLOC_NOTE}", ""]
    return "\n".join(lines)


def write(run: RunReport, output_dir: Path) -> tuple[Path, Path]:
    """Write ``<timestamp>.json`` and ``<timestamp>.md``; return their paths."""
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = run.started_at.strftime("%Y%m%dT%H%M%SZ")
    json_path = output_dir / f"{stem}.json"
    md_path = output_dir / f"{stem}.md"
    json_path.write_bytes(msgspec.json.format(msgspec.json.encode(run), indent=2) + b"\n")
    md_path.write_text(to_markdown(run) + "\n")
    return json_path, md_path


def print_registry(registry: Registry, console: Console | None = None) -> None:
    console = console or _console()
    table = Table(title="Available drivers", title_justify="left")
    for header in ("name", "kind", "rows", "package", "notes"):
        table.add_column(header)
    for spec in registry.specs.values():
        table.add_row(spec.name, kind_of(spec), spec.rows, spec.package, spec.notes)
    console.print(table)
    for module, reason in registry.unavailable.items():
        console.print(f"[yellow]unavailable:[/yellow] drivers/{module}.py ({reason})")
