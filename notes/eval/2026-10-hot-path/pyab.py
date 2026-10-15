#!yeet
"""Interleaved runs of pyover.py over builds (see ab.py); median/min of the per-run medians.

    uv run yeet ./notes/eval/2026-10-hot-path/pyab.py base new --pairs 5
"""

import os
import re
import statistics
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
YEET = str(Path(sys.executable).parent / "yeet")


def main(variants: list[str], *, root: str = "/tmp/fastpg-variants", pairs: int = 5) -> None:
    res: dict[str, list[float]] = {v: [] for v in variants}
    for i in range(pairs):
        for v in variants if i % 2 == 0 else list(reversed(variants)):
            args = [YEET, str(HERE / "pyover.py"), "-n", "100000", "--rounds", "5"]
            env = dict(os.environ, PYTHONPATH=str(Path(root) / v))
            out = subprocess.run(args, env=env, capture_output=True, text=True, check=True).stdout
            match = re.search(r"median (\d+) ns", out)
            assert match
            res[v].append(float(match[1]))
    for v, xs in res.items():
        print(f"{v:>14}: median {statistics.median(xs):.0f} ns  min {min(xs):.0f}  max {max(xs):.0f}")
