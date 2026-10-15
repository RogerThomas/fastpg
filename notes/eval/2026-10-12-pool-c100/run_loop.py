import os
import sys
from pathlib import Path

sys.path.insert(0, "/src/bench")
import harness

harness.LOOP = os.environ["BENCH_LOOP"]  # type: ignore[assignment]
import run

run.main(
    drivers=["fastpg-struct", "asyncpg-struct", "psycopg-async-struct", "psqlpy-struct"],
    scenarios=["concurrent"],
    duration=3.0,
    concurrency=int(os.environ["BENCH_C"]),
    pool_size=16,
    output_dir=Path("/out"),
    dsn=os.environ["FASTPG_DSN"],
)
