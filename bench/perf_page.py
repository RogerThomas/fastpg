#!yeet
"""Rebuild the Benchmarks page's chart and table (``docs/performance.md``) from a compare JSON.

Rewrites only what sits between the page's ``bench-chart:start`` and ``bench-chart:end``
markers: the headline, the smallest lead, the bar chart and the table under it. The chart's
bars are each driver's median throughput over the fastest other driver's, on one scale from
0 to ``SCALE``; its colours are in ``docs/stylesheets/extra.css``.

    uv run yeet ./bench/perf_page.py bench/results/aws-<run id>/x86_64/compare-<timestamp>.json
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import msgspec
from comparison import report
from yeetr import Arg

PAGE = Path(__file__).resolve().parent.parent / "docs" / "performance.md"
START = "<!-- bench-chart:start -->"
END = "<!-- bench-chart:end -->"

# Every workload's name on the page.
NAMES: dict[str, str] = {
    "point-c1": "Primary-key lookup, 1 task",
    "point-c16": "Primary-key lookup, 16 tasks",
    "point-c64": "Primary-key lookup, 64 tasks",
    "point-c16-typed": "Primary-key lookup into a Struct, 16 tasks",
    "page-20-c16": "Page of 20 rows, 16 tasks",
    "page-20-c16-typed": "Page of 20 rows into Structs, 16 tasks",
    "fetch-1000-c4": "1,000 rows, 4 tasks",
    "fetch-1000-c4-typed": "1,000 rows into Structs, 4 tasks",
    "wide-50-c16": "50 wide rows (13 columns), 16 tasks",
    "wide-50-c16-typed": "50 wide rows into Structs, 16 tasks",
    "insert-c16": "Single-row INSERT, 16 tasks",
    "update-c16": "Single-row UPDATE, 16 tasks",
    "transaction-c16": "Transaction of 4 statements, 16 tasks",
    "executemany-1000": "`execute_many`, 1,000 rows",
    "copy-5000": "Binary COPY of 5,000 rows",
}
# The chart's and the table's workloads, top to bottom, and what the headline calls them.
CHART = (
    "point-c16-typed",
    "point-c64",
    "page-20-c16-typed",
    "fetch-1000-c4-typed",
    "insert-c16",
    "update-c16",
)
CHART_SUMMARY = "primary-key lookups, pages of rows, 1,000-row fetches and single-row writes"
# Each chart group's rows, top to bottom: configuration and label.
ROWS = (
    ("fastpg", "fastpg"),
    ("asyncpg-noreset", "asyncpg"),
    ("psycopg", "psycopg"),
    ("psqlpy", "psqlpy"),
)
SCALE = 3.0  # the bars' scale runs from 0 to this many times the fastest other driver
ROWS_PER_S_FROM = 1000  # workloads with this many rows per op show rows/s, not queries/s


def _label(config: str) -> str:
    return config.removesuffix("-noreset")


def _ratio(value: float) -> str:
    return f"{value:.2f}x"


@dataclass(frozen=True, slots=True)
class _Workload:
    """One workload's medians: fastpg's, and every other driver's."""

    name: str
    rows_per_op: int
    ops: dict[str, float]  # configuration -> median ops/s
    rows: dict[str, float]  # configuration -> median rows/s

    @property
    def title(self) -> str:
        return NAMES[self.name]

    @property
    def best(self) -> str:
        """The fastest other driver's configuration."""
        others = {c: ops for c, ops in self.ops.items() if not c.startswith("fastpg")}
        return max(others, key=others.__getitem__)

    def lead(self, config: str = "fastpg") -> float:
        """``config``'s median throughput over the fastest other driver's."""
        return self.ops[config] / self.ops[self.best]

    def throughput(self, config: str, unit: str) -> str:
        if self.rows_per_op < ROWS_PER_S_FROM:
            return f"{self.ops[config]:,.0f}{unit}"
        rows = self.rows[config]
        number = f"{rows / 1e6:.2f}M" if rows >= 1e6 else f"{rows / 1e3:.0f}k"
        return f"{number} rows/s"


def _workloads(data: report.Report) -> dict[str, _Workload]:
    """The workloads fastpg and at least one other driver ran, without failures."""
    out: dict[str, _Workload] = {}
    for info in data.workloads:
        aggs = [
            a
            for a in data.aggregates
            if a.workload == info.name and a.ops_per_s is not None and not a.failed
        ]
        ops = {a.config: a.ops_per_s.median for a in aggs if a.ops_per_s is not None}
        rows = {a.config: a.rows_per_s for a in aggs if a.rows_per_s is not None}
        if "fastpg" in ops and any(not c.startswith("fastpg") for c in ops):
            out[info.name] = _Workload(info.name, info.rows_per_op, ops, rows)
    return out


def _row(workload: _Workload, config: str, label: str) -> str:
    amount = workload.throughput(config, " queries/s")
    if config == "fastpg":
        versus = f"{_ratio(workload.lead())} of {_label(workload.best)}"
    else:
        versus = f"{_ratio(workload.ops[config] / workload.ops['fastpg'])} of fastpg"
    width = workload.lead(config) / SCALE * 100
    value = f'<span class="bench-value">{_ratio(workload.lead())}</span>'
    return (
        f'    <div class="bench-row{" bench-row--fastpg" if config == "fastpg" else ""}"'
        f' title="{label}: {amount} ({versus})">'
        f'<span class="bench-name">{label}</span>'
        f'<span class="bench-track"><span class="bench-bar" style="width: {width:.2f}%">'
        f"{value if config == 'fastpg' else ''}</span></span></div>"
    )


def _chart(chart: list[_Workload]) -> list[str]:
    leads = [w.lead() for w in chart]
    summary = (
        f"Bar chart of throughput relative to the fastest other driver: fastpg does"
        f" {_ratio(min(leads))} to {_ratio(max(leads))} on each of the six workloads in the"
        " table below."
    )
    lines = [
        f'<div class="bench-chart" role="img" aria-label="{summary}">',
        '  <div class="bench-axis">Throughput, relative to the fastest other driver (1x)</div>',
    ]
    for workload in chart:
        lines += [
            '  <div class="bench-group">',
            f'    <div class="bench-caption">{workload.title}</div>',
            *(_row(workload, config, label) for config, label in ROWS),
            "  </div>",
        ]
    return [*lines, "</div>"]


def _table(chart: list[_Workload]) -> list[str]:
    lines = [
        "| workload | speed-up | fastpg | fastest other driver |",
        "|---|---:|---:|---:|",
    ]
    lines += [
        f"| {w.title} | **{_ratio(w.lead())}** | {w.throughput('fastpg', '/s')}"
        f" | {w.throughput(w.best, '/s')} ({_label(w.best)}) |"
        for w in chart
    ]
    return lines


def render(data: report.Report) -> str:
    """The page's text between the markers."""
    workloads = _workloads(data)
    chart = [workloads[name] for name in CHART]
    for workload in chart:
        if workload.lead() > SCALE:
            msg = f"fastpg leads {workload.name} by more than {SCALE}x: raise SCALE"
            raise SystemExit(msg)
    leads = [w.lead() for w in chart]
    smallest = min(workloads.values(), key=_Workload.lead)
    name = smallest.title[0].lower() + smallest.title[1:]
    span = f"{min(leads):.1f}\N{EN DASH}{max(leads):.1f}x"
    lines = [
        f"**On {CHART_SUMMARY}, fastpg does {span} the work of the fastest other driver.**",
        "",
        f"Its smallest lead is {name}: {_ratio(smallest.lead())}.",
        "",
        *_chart(chart),
        "",
        *_table(chart),
    ]
    return "\n".join(lines)


def main(
    compare_json: Annotated[Path, Arg(help="A compare-<timestamp>.json", exists=True)],
) -> None:
    """Rewrite the Benchmarks page's chart and table from ``compare_json``."""
    data = msgspec.json.decode(compare_json.read_bytes(), type=report.Report)
    page = PAGE.read_text()
    head, start, rest = page.partition(START)
    _, end, tail = rest.partition(END)
    if not start or not end:
        msg = f"{PAGE} has no {START} ... {END} region"
        raise SystemExit(msg)
    PAGE.write_text(f"{head}{START}\n\n{render(data)}\n\n{END}{tail}")
