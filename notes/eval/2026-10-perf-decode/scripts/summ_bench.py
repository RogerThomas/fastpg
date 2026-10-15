#!yeet
"""Medians of run.py rows/s (JSON results) and decode_cost.py ns/row (tables) per variant."""

import json
import re
import statistics
from collections import defaultdict
from pathlib import Path

SP = Path(__file__).parent


def _runpy(variant: str) -> dict[tuple[str, str], list[float]]:
    out: dict[tuple[str, str], list[float]] = defaultdict(list)
    for path in sorted((SP / "runpy" / variant).glob("*.json")):
        for r in json.loads(path.read_text())["results"]:
            out[(r["driver"], r["scenario"])].append(r["rows_per_s"])
    return out


def _decode(variant: str) -> dict[tuple[str, str], list[float]]:
    out: dict[tuple[str, str], list[float]] = defaultdict(list)
    row = re.compile(r"│ (\S+)\s+│\s+([\d,]+) │\s+[\d,]+ │\s+[\d,]+ │\s+([\d,]+) │")
    for line in (SP / f"decode_cost_{variant}.txt").read_text().splitlines():
        m = row.search(line)
        if m:
            out[(m[1], m[2])].append(float(m[3].replace(",", "")))
    return out


def main() -> None:
    for name, fn, unit in (("run.py rows/s", _runpy, ""), ("decode_cost ns/row", _decode, "")):
        base, final = fn("baseline"), fn("final")
        print(f"== {name}")
        for key in sorted(base):
            b, f = base[key], final.get(key, [])
            if not f:
                continue
            mb, mf = statistics.median(b), statistics.median(f)
            print(
                f"{key[0]:16} {key[1]:10} baseline {mb:12,.0f} ({min(b):,.0f}-{max(b):,.0f})  "
                f"final {mf:12,.0f} ({min(f):,.0f}-{max(f):,.0f})  {(mf / mb - 1) * 100:+6.1f}%{unit}"
            )
