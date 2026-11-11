#!yeet
"""Rebuild the speed charts on the Benchmarks page and the home page from a compare JSON.

On the Benchmarks page (``docs/performance.md``) it rewrites only what sits between the
``bench-chart:start`` and ``bench-chart:end`` markers: the headline, the smallest lead, the bar
chart and the table under it. On the home page (``docs/index.md``) it rewrites what sits
between ``home-speed:start`` and ``home-speed:end``: the same headline, a smaller chart and
the smallest lead. The chart's bars are each driver's median throughput over the fastest other
driver's, on one scale from 0 to ``SCALE``; its colours are in ``docs/stylesheets/extra.css``.

    uv run yeet ./bench/perf_page.py bench/results/aws-<run id>/x86_64/compare-<timestamp>.json
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import msgspec
from comparison import report
from yeetr import Arg

DOCS = Path(__file__).resolve().parent.parent / "docs"

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
# The home page's smaller chart: the Benchmarks chart's workloads with the biggest leads.
HOME_CHART_SIZE = 2
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
    value = f'<span class="bench-value">{_ratio(workload.lead(config))}</span>'
    return (
        f'    <div class="bench-row bench-row--{label}" title="{label}: {amount} ({versus})">'
        f'<span class="bench-name">{label}</span>'
        f'<span class="bench-track"><span class="bench-bar" style="width: {width:.2f}%">'
        f"{value}</span></span></div>"
    )


def _chart(chart: list[_Workload], pool_size: int, *, table: bool) -> list[str]:
    leads = [w.lead() for w in chart]
    count = ("zero", "one", "two", "three", "four", "five", "six")[len(chart)]
    where = " in the table below" if table else ""
    summary = (
        f"Bar chart of throughput relative to the fastest other driver: fastpg does"
        f" {_ratio(min(leads))} to {_ratio(max(leads))} on each of the {count} workloads{where}."
    )
    lines = [
        f'<div class="bench-chart" role="img" aria-label="{summary}">',
        '  <div class="bench-axis">',
        "    Throughput, relative to the fastest other driver (1x).<br>",
        (
            "    A task is one asyncio task sending queries one after another. The tasks run at"
            f" the same time, sharing one pool of {pool_size} connections per driver."
        ),
        "  </div>",
    ]
    for workload in chart:
        lines += [
            '  <div class="bench-group">',
            f'    <div class="bench-caption">{workload.title}</div>',
            # Fastest first: each group reads as a ranking, its winner under the heading.
            *(
                _row(workload, config, label)
                for config, label in sorted(ROWS, key=lambda row: -workload.ops[row[0]])
            ),
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


def _sorted(workloads: dict[str, _Workload], names: tuple[str, ...]) -> list[_Workload]:
    """``names``' workloads, biggest lead first."""
    chart = sorted((workloads[name] for name in names), key=lambda w: -w.lead())
    for workload in chart:
        if workload.lead() > SCALE:
            msg = f"fastpg leads {workload.name} by more than {SCALE}x: raise SCALE"
            raise SystemExit(msg)
    return chart


def _headline(workloads: dict[str, _Workload]) -> str:
    """The bold line over both pages' charts: fastpg's lead across the Benchmarks chart."""
    leads = [w.lead() for w in _sorted(workloads, CHART)]
    span = f"{min(leads):.1f}\N{EN DASH}{max(leads):.1f}x"
    return f"**On {CHART_SUMMARY}, fastpg does {span} the work of the fastest other driver.**"


def _smallest(workloads: dict[str, _Workload]) -> str:
    smallest = min(workloads.values(), key=_Workload.lead)
    name = smallest.title[0].lower() + smallest.title[1:]
    return f"Its smallest lead is {name}: {_ratio(smallest.lead())}."


def render(data: report.Report) -> str:
    """The Benchmarks page's text between its markers."""
    workloads = _workloads(data)
    chart = _sorted(workloads, CHART)
    lines = [
        _headline(workloads),
        "",
        _smallest(workloads),
        "",
        *_chart(chart, data.settings.pool_size, table=True),
        "",
        "## The raw numbers",
        "",
        *_table(chart),
        "",
        "",
    ]
    return "\n".join(lines)


def render_home(data: report.Report) -> str:
    """The home page's text between its markers: the headline and the smaller chart."""
    workloads = _workloads(data)
    lines = [
        _headline(workloads),
        "",
        *_chart(
            _sorted(workloads, CHART)[:HOME_CHART_SIZE],
            data.settings.pool_size,
            table=False,
        ),
        "",
    ]
    return "\n".join(lines)


def _rewrite(page: Path, region: str, text: str) -> None:
    """Replace what sits between ``page``'s ``region`` markers with ``text``."""
    start, end = f"<!-- {region}:start -->", f"<!-- {region}:end -->"
    head, found_start, rest = page.read_text().partition(start)
    _, found_end, tail = rest.partition(end)
    if not found_start or not found_end:
        msg = f"{page} has no {start} ... {end} region"
        raise SystemExit(msg)
    page.write_text(f"{head}{start}\n\n{text}{end}{tail}")


def main(
    compare_json: Annotated[Path, Arg(help="A compare-<timestamp>.json", exists=True)],
) -> None:
    """Rewrite the Benchmarks page's and the home page's speed charts from ``compare_json``."""
    data = msgspec.json.decode(compare_json.read_bytes(), type=report.Report)
    _rewrite(DOCS / "performance.md", "bench-chart", render(data))
    _rewrite(DOCS / "index.md", "home-speed", render_home(data))
