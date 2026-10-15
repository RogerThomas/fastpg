#!yeet
"""Summarize the interleaved BASE/NEW runs: median (min-max) over rounds and % change."""

import glob
import json
import re
import statistics
from pathlib import Path

RAW = Path(__file__).parent / "raw"
SIZES = [1_000, 10_000, 100_000]


def runs(name: str, side: str) -> list[Path]:
    return sorted(RAW.glob(f"r[0-9]-{name}-{side}"))


def runpy(d: Path) -> dict[tuple[str, str], dict]:
    f = next(d.glob("2026*.json"))
    return {(r["driver"], r["scenario"]): r for r in json.loads(f.read_text())["results"]}


def jsonfile(d: Path, pat: str) -> list[dict]:
    return json.loads(next(d.glob(pat)).read_text())


def cpureq(d: Path) -> dict[str, float]:
    for line in (d / "stdout.txt").read_text().splitlines():
        if "fastpg-struct" in line:
            c = [x.strip().replace(",", "") for x in line.split("│")[1:-1]]
            return {"p50": float(c[1]), "p99": float(c[2]), "cpu": float(c[3]), "rps": float(c[6])}
    raise ValueError(d)


def writes_base(d: Path) -> dict[tuple[str, int], float]:
    out: dict[tuple[str, int], float] = {}
    count: dict[str, int] = {}
    for line in (d / "stdout.txt").read_text().splitlines():
        if line.startswith("│"):
            c = [x.strip() for x in line.split("│")[1:-1]]
            m = c[0]
            i = count.get(m, 0)
            count[m] = i + 1
            rows = SIZES[i]
            out[(m, rows)] = rows / (float(c[2].replace(",", "")) / 1e3)
    return out


def writes_new(d: Path) -> dict[tuple[str, int], float]:
    best: dict[tuple[str, int], float] = {}
    for r in json.loads((d / "writes.json").read_text()):
        k = (r["method"], r["rows"])
        best[k] = max(best.get(k, 0.0), r["rows"] / r["wall"])
    return best


def fmt(v: float) -> str:
    if abs(v) >= 1000:
        return f"{v:,.0f}"
    if abs(v) >= 100:
        return f"{v:.0f}"
    return f"{v:.1f}"


def cell(vals: list[float]) -> str:
    if not vals:
        return "n/a"
    return f"{fmt(statistics.median(vals))} ({fmt(min(vals))}–{fmt(max(vals))})"


def row(label: str, b: list[float], n: list[float]) -> str:
    pct = (
        f"{(statistics.median(n) / statistics.median(b) - 1) * 100:+.1f}%" if b and n else "–"
    )
    return f"| {label} | {cell(b)} | {cell(n)} | {pct} | {len(b)}/{len(n)} |"


def header(title: str) -> None:
    print(f"\n### {title}\n\n| metric | BASE | NEW | change | n |\n|---|---|---|---|---|")


def collect(name: str, fn) -> tuple[list, list]:
    return [fn(d) for d in runs(name, "base")], [fn(d) for d in runs(name, "new")]


def main() -> None:
    header("1. Pool, closed loop (--cpus 1), ops/s and p99 µs")
    for name, c in (("conc16", 16), ("conc100", 100)):
        b, n = collect(name, runpy)
        for sc in ("concurrent", "concurrent-pinned"):
            k = ("fastpg-struct", sc)
            for m, lab in (("ops_per_s", "ops/s"), ("p50_us", "p50 µs"), ("p99_us", "p99 µs")):
                print(row(f"{sc} c{c} {lab}", [x[k][m] for x in b], [x[k][m] for x in n]))
    b, n = collect("pipe16", runpy)
    for drv in ("fastpg-pipelined", "asyncpg-struct"):
        k = (drv, "concurrent")
        for m, lab in (("ops_per_s", "ops/s"), ("p50_us", "p50 µs"), ("p99_us", "p99 µs")):
            print(row(f"{drv} concurrent c16 {lab}", [x[k][m] for x in b if k in x], [x[k][m] for x in n if k in x]))

    header("2. Sequential point lookup, fastpg-struct")
    for name, lab in (("point-cpus1", "--cpus 1"), ("point-nolimit", "no limit")):
        b, n = collect(name, runpy)
        k = ("fastpg-struct", "point")
        for m, ml in (("ops_per_s", "ops/s"), ("p50_us", "p50 µs"), ("p99_us", "p99 µs")):
            print(row(f"{lab} {ml}", [x[k][m] for x in b], [x[k][m] for x in n]))

    header("3. CPU per request (cpu_per_request.py, sequential, --cpus 1)")
    for name in ("cpureq", "cpureq50k"):
        b, n = collect(name, cpureq)
        if not b:
            continue
        for m, ml in (("cpu", "process CPU µs/req"), ("p50", "p50 µs"), ("p99", "p99 µs"), ("rps", "req/s")):
            print(row(f"{name} {ml}", [x[m] for x in b], [x[m] for x in n]))

    header("4. Big results (--cpus 1), rows/s")
    b, n = collect("range", runpy)
    for drv in ("fastpg-struct", "fastpg-pydantic"):
        for sc in ("range-1k", "range-10k"):
            k = (drv, sc)
            print(row(f"{drv} {sc}", [x[k]["rows_per_s"] for x in b], [x[k]["rows_per_s"] for x in n]))

    header("5. Writes (--cpus 1), rows/s, best of 3 per run")
    b = [writes_base(d) for d in runs("writes", "base")]
    n = [writes_new(d) for d in runs("writes", "new")]
    methods = list(dict.fromkeys(m for m, _ in n[0])) if n else []
    for m in methods:
        for rows in SIZES:
            k = (m, rows)
            print(row(f"{m} {rows:,}", [x[k] for x in b if k in x], [x[k] for x in n if k in x]))

    header("6. Mixed (--cpus 1), µs")
    b, n = collect("mixed", lambda d: jsonfile(d, "mixed-*.json"))
    def pick(lst, cfg, rate, m):
        return [next(r[m] for r in x if r["config"] == cfg and r["rate"] == rate) for x in lst]
    for cfg in ("fastpg-pool x16 cap 8", "fastpg-pool x100 cap 8"):
        for rate in (5000, 10000, 20000):
            for m in ("fast_p50_us", "fast_p99_us", "fast_p999_us", "slow_p50_us", "slow_p99_us", "achieved_rps"):
                print(row(f"{cfg} {rate} {m}", pick(b, cfg, rate, m), pick(n, cfg, rate, m)))

    header("7. Open loop fastpg-struct (--cpus 1), µs")
    b, n = collect("openloop", lambda d: jsonfile(d, "open-loop-*.json"))
    def pk(lst, rate, m):
        return [next(r[m] for r in x if r["target_rps"] == rate) for x in lst]
    for rate in (1000, 5000, 10000):
        for m in ("p50_us", "p99_us", "p999_us", "lag_p99_us", "loop_busy_pct", "achieved_rps"):
            print(row(f"{rate}/s {m}", pk(b, rate, m), pk(n, rate, m)))
