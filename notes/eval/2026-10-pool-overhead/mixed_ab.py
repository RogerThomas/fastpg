"""Interleaved A/B of bench/mixed.py across _pool.py variants (swapped into src, restored).

usage: python3 mixed_ab.py OUTDIR REPEATS VARIANT... -- MIXED_ARGS...
"""

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

R = Path("<repo>")
V = Path(__file__).resolve().parent / "v"
POOL = R / "src/fastpg/_pool.py"
RESULTS = R / "bench/results"

args = sys.argv[1:]
sep = args.index("--")
out = Path(args[0])
repeats = int(args[1])
variants = args[2:sep]
mixed_args = args[sep + 1 :]
out.mkdir(parents=True, exist_ok=True)
head = POOL.read_text()
try:
    for rep in range(repeats):
        for name in variants:
            POOL.write_text((V / f"{name}.py").read_text())
            before = set(RESULTS.glob("mixed-*.json"))
            cmd = ["uv", "run", "--no-sync", "--project", str(R), "yeet", str(R / "bench/mixed.py"), *mixed_args]
            proc = subprocess.run(cmd, capture_output=True, text=True, cwd=R, env={
                **__import__("os").environ,
                "FASTPG_DSN": "postgresql://fastpg:fastpg@localhost:55432/fastpg?sslmode=disable",
            })
            new = sorted(set(RESULTS.glob("mixed-*.json")) - before)
            if proc.returncode or not new:
                print(name, rep, "FAILED", proc.stderr[-2000:])
                continue
            rows = json.loads(new[-1].read_text())
            for f in new:
                f.unlink()
            for t in RESULTS.glob("mixed-throughput-*.json"):
                if t not in before:
                    t.unlink()
            (out / f"{name}-{rep}.json").write_text(json.dumps(rows, indent=1))
            for r in rows:
                print(
                    f"{name:8s} rep{rep} {r['config']:24s} {r['rate']:>6} achieved {r['achieved_rps']:8.0f}"
                    f" fast p50 {r['fast_p50_us']:8.0f} p99 {r['fast_p99_us']:8.0f} p99.9 {r['fast_p999_us']:8.0f}"
                    f" slow p50 {r['slow_p50_us']:8.0f} p99 {r['slow_p99_us']:8.0f}",
                    flush=True,
                )
            time.sleep(1)
finally:
    POOL.write_text(head)
