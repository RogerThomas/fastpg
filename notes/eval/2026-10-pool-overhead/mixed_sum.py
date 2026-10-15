"""Summarise mixed_ab.py output: per variant/config/rate, median [min..max] over repeats."""

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

d = Path(sys.argv[1])
cells: dict[tuple[str, str, int], list[dict[str, float]]] = defaultdict(list)
order: list[str] = []
for f in sorted(d.glob("*.json")):
    name = f.stem.rsplit("-", 1)[0]
    if name not in order:
        order.append(name)
    for r in json.loads(f.read_text()):
        cells[(r["config"], name, r["rate"])].append(r)


def fmt(vals: list[float]) -> str:
    m = statistics.median(vals)
    f = (lambda v: f"{v / 1000:.1f}ms") if m >= 1000 else (lambda v: f"{v:.0f}µs")
    return f"{f(m)} [{f(min(vals))}..{f(max(vals))}]"


print("| config | rate | variant | fast p50 | fast p99 | fast p99.9 | slow p99 |")
print("|---|---:|---|---|---|---|---|")
for (config, name, rate) in sorted(cells, key=lambda k: (k[0], k[2], order.index(k[1]))):
    rs = cells[(config, name, rate)]
    print(
        f"| {config} | {rate // 1000}k | {name} | {fmt([r['fast_p50_us'] for r in rs])} | "
        f"{fmt([r['fast_p99_us'] for r in rs])} | {fmt([r['fast_p999_us'] for r in rs])} | "
        f"{fmt([r['slow_p99_us'] for r in rs])} |"
    )
