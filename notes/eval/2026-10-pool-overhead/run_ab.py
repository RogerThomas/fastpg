"""Interleaved A/B of bench/run.py (concurrent scenarios) across _pool.py variants.

usage: python3 run_ab.py OUTDIR REPEATS VARIANT... -- RUN_ARGS...
Each variant file is swapped into src/fastpg/_pool.py for its run; HEAD's file is restored.
"""

import json
import os
import shutil
import statistics
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

R = Path("<repo>")
V = Path(__file__).resolve().parent / "v"
POOL = R / "src/fastpg/_pool.py"

args = sys.argv[1:]
sep = args.index("--")
out = Path(args[0])
repeats = int(args[1])
variants = args[2:sep]
run_args = args[sep + 1 :]
head = POOL.read_text()
ops: dict[tuple[str, str], list[float]] = defaultdict(list)
try:
    for rep in range(repeats):
        for name in variants:
            POOL.write_text((V / f"{name}.py").read_text())
            od = out / f"{name}-{rep}"
            if od.exists():
                shutil.rmtree(od)
            od.mkdir(parents=True)
            cmd = ["uv", "run", "--no-sync", "--project", str(R), "yeet", str(R / "bench/run.py"),
                   *run_args, "-o", str(od)]
            env = {**os.environ, "FASTPG_DSN": "postgresql://fastpg:fastpg@localhost:55432/fastpg?sslmode=disable"}
            proc = subprocess.run(cmd, capture_output=True, text=True, cwd=R, env=env)
            files = list(od.glob("*.json"))
            if proc.returncode or not files:
                print(name, rep, "FAILED", proc.stderr[-2000:], flush=True)
                continue
            for r in json.loads(files[0].read_text())["results"]:
                ops[(name, r["scenario"])].append(r["ops_per_s"])
                print(f"{name:8s} rep{rep} {r['scenario']:18s} {r['ops_per_s']:9.0f} ops/s p50 {r['p50_us']:7.1f}", flush=True)
finally:
    POOL.write_text(head)
for (name, scenario), v in sorted(ops.items()):
    print(f"{name:8s} {scenario:18s} median {statistics.median(v):9.0f} [{min(v):.0f}..{max(v):.0f}]")
