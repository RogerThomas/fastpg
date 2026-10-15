#!yeet
"""Load the test schema and seed (docker/initdb/*.sql) into an empty database.

Run: ./scripts/seed_test_db.py postgresql://user:password@host:5432/database
"""

from pathlib import Path

import fastpg


async def main(dsn: str) -> None:
    """Run each docker/initdb script, in name order, as one script."""
    initdb = Path(__file__).resolve().parent.parent / "docker" / "initdb"
    async with await fastpg.connect(dsn) as conn:
        for script in sorted(initdb.glob("*.sql")):
            await conn.execute_script(script.read_text())
            print(f"loaded {script.name}")
