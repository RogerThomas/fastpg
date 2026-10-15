#!yeet
"""Summarise ab.py's JSONL: per (workload, metric, variant) the median and min-max over rounds,
and the median paired delta vs the base variant (same round), in %, with its min-max."""

import json
import statistics
from pathlib import Path

HERE = Path(__file__).parent
SKIP = {"round", "variant", "workload", "fastpg", "loop", "requests", "wall", "loop_cpu_us"}


def main(*files: str, base: str = "base") -> None:
    records = [json.loads(line) for f in files for line in (HERE / f).read_text().splitlines()]
    cells: dict[tuple[str, str], dict[str, dict[int, float]]] = {}
    for r in records:
        for key, value in r.items():
            if key in SKIP or not isinstance(value, (int, float)):
                continue
            metric = (r["workload"], key)
            cells.setdefault(metric, {}).setdefault(r["variant"], {})[r["round"]] = float(value)
    for (workload, key), per_variant in cells.items():
        print(f"\n== {workload}: {key}")
        ref = per_variant.get(base, {})
        for variant, by_round in per_variant.items():
            vals = sorted(by_round.values())
            line = (
                f"  {variant:>10}: median {statistics.median(vals):10.3f}"
                f"  min-max {vals[0]:9.3f}-{vals[-1]:9.3f}  n={len(vals)}"
            )
            if variant != base and ref:
                deltas = sorted(
                    100 * (v - ref[i]) / ref[i] for i, v in by_round.items() if i in ref
                )
                line += (
                    f"  | paired vs {base}: median {statistics.median(deltas):+6.1f}%"
                    f"  min-max {deltas[0]:+6.1f}..{deltas[-1]:+6.1f}%"
                )
            print(line)
