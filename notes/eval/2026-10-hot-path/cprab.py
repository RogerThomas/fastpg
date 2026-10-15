#!yeet
"""Interleaved runs of ``bench/cpu_per_request.py`` over builds (see ``ab.py`` for variants).

Run from the repository root:

    uv run yeet ./notes/eval/2026-10-hot-path/cprab.py base new --pairs 6 --requests 5000
    uv run yeet ./notes/eval/2026-10-hot-path/cprab.py base new --tasks 16 -d fastpg-pipelined
"""

import os
import re
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Annotated

from yeetr import Opt

YEET = str(Path(sys.executable).parent / "yeet")
ROW = re.compile(r"│ (\S+)\s+│\s+(\S+) │\s+(\S+) │\s+(\S+) │\s+(\S+) │\s+(\S+) │\s+(\S+) │")


def main(
    variants: list[str],
    *,
    root: str = "/tmp/fastpg-variants",
    pairs: int = 5,
    requests: int = 5000,
    tasks: int = 0,
    driver: Annotated[str, Opt("-d")] = "fastpg-struct",
) -> None:
    cpu: dict[str, list[float]] = {v: [] for v in variants}
    ops: dict[str, list[float]] = {v: [] for v in variants}
    for i in range(pairs):
        for v in variants if i % 2 == 0 else list(reversed(variants)):
            env = dict(os.environ, PYTHONPATH=str(Path(root) / v))
            args = [YEET, "./bench/cpu_per_request.py", "-d", driver, "-n", str(requests)]
            if tasks:
                args += ["-t", str(tasks)]
            out = subprocess.run(args, env=env, capture_output=True, text=True, check=True)
            match = next(m for line in out.stdout.splitlines() if (m := ROW.search(line)))
            cpu[v].append(float(match[4].replace(",", "")))
            ops[v].append(float(match[7].replace(",", "")))
    for v in variants:
        c, o = sorted(cpu[v]), sorted(ops[v])
        print(
            f"{v:>12}: process CPU µs/req median {statistics.median(c):.1f}"
            f" (min {c[0]:.1f}, max {c[-1]:.1f})"
            f" | req/s median {statistics.median(o):,.0f} (min {o[0]:,.0f}, max {o[-1]:,.0f})",
        )
