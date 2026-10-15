#!yeet
"""Interleaved A/B of hot.py over builds: each variant is a copy of ``src/fastpg`` (Python
sources plus the built extension) in ``<root>/<variant>/fastpg``, put first on PYTHONPATH.

Runs alternate (A B, B A, ...) so host drift hits every variant alike. Prints per-variant
medians and IQRs over all rounds, and the paired difference to the first variant.

    uv sync --reinstall-package fastpg && cp -R src/fastpg /tmp/fastpg-variants/base/
    ... (change, rebuild, copy to /tmp/fastpg-variants/new/) ...
    uv run yeet ./notes/eval/2026-10-hot-path/ab.py base new --pairs 8 -n 4000 --mode struct
"""

import json
import os
import statistics
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
YEET = str(Path(sys.executable).parent / "yeet")


def _one(root: Path, variant: str, n: int, rounds: int, mode: str) -> list[list[float]]:
    env = dict(os.environ, PYTHONPATH=str(root / variant), HOT_JSON="1")
    args = [YEET, str(HERE / "hot.py"), "-n", str(n), "--rounds", str(rounds), "--mode", mode]
    out = subprocess.run(args, env=env, capture_output=True, text=True, check=True)
    line = next(line for line in out.stdout.splitlines() if line.startswith("JSON "))
    return json.loads(line[5:])


def _fmt(vals: list[float]) -> str:
    vals = sorted(vals)
    q1, q3 = vals[len(vals) // 4], vals[(3 * len(vals)) // 4]
    return f"median {statistics.median(vals):6.2f}  IQR {q1:6.2f}-{q3:6.2f}  min {vals[0]:6.2f}"


def main(
    variants: list[str],
    *,
    root: str = "/tmp/fastpg-variants",
    pairs: int = 8,
    n: int = 4000,
    rounds: int = 3,
    mode: str = "struct",
) -> None:
    per: dict[str, list[list[float]]] = {v: [] for v in variants}
    for i in range(pairs):
        for v in variants if i % 2 == 0 else list(reversed(variants)):
            per[v].extend(_one(Path(root), v, n, rounds, mode))
    base = variants[0]
    for v in variants:
        rs = per[v]
        cpu = [r[0] + r[1] for r in rs]
        print(f"{v:>14}: cpu {_fmt(cpu)} | user {_fmt([r[0] for r in rs])} | sys {_fmt([r[1] for r in rs])}")
        ops = sorted(1e6 / r[3] for r in rs)
        print(f"{'':>14}  ops/s median {statistics.median(ops):,.0f}  IQR {ops[len(ops) // 4]:,.0f}-{ops[3 * len(ops) // 4]:,.0f}")
        if v != base:
            d = [(b[0] + b[1]) - (a[0] + a[1]) for a, b in zip(per[base], rs, strict=True)]
            du = [b[0] - a[0] for a, b in zip(per[base], rs, strict=True)]
            print(f"{'':>14}  paired cpu delta vs {base}: {_fmt(d)} | user delta {_fmt(du)}")
