#!yeet
"""Median and spread (min-max, IQR) per (label, target, rows) from ab.py JSON lines."""

import json
import statistics
from collections import defaultdict
from pathlib import Path


def _q(values: list[float]) -> tuple[float, float, float]:
    if len(values) < 4:
        return statistics.median(values), min(values), max(values)
    q = statistics.quantiles(values, n=4)
    return q[1], q[0], q[2]


def main(path: Path) -> None:
    groups: dict[tuple[str, int, str], list[dict[str, float]]] = defaultdict(list)
    order: list[str] = []
    for line in path.read_text().splitlines():
        if not line.startswith("{"):
            continue
        d = json.loads(line)
        if d["label"] not in order:
            order.append(d["label"])
        groups[(d["target"], d["rows"], d["label"])].append(d)
    keys = sorted({(t, n) for t, n, _ in groups})
    print(f"{'target':8} {'rows':>6} {'variant':22} {'n':>3} {'ns/row med':>10} {'IQR':>13} "
          f"{'min-max':>13} {'rows/s med':>11} {'gc ns/row':>9} {'vs first':>8}")
    for t, n in keys:
        base = None
        for label in order:
            runs = groups.get((t, n, label))
            if not runs:
                continue
            cpu = [r["cpu_ns_row"] for r in runs]
            wall = [r["wall_rows_s"] for r in runs]
            med, lo, hi = _q(cpu)
            base = base or med
            print(f"{t:8} {n:6d} {label:22} {len(cpu):3d} {med:10.0f} {lo:6.0f}-{hi:<6.0f} "
                  f"{min(cpu):6.0f}-{max(cpu):<6.0f} {statistics.median(wall):11,.0f} "
                  f"{statistics.median(r.get('gc_ns_row', 0) for r in runs):9.0f} "
                  f"{(med / base - 1) * 100:+7.1f}%")
