"""A quick check of an installed fastpg wheel, which the release workflow runs on each wheel it
makes, in an environment with nothing but the wheel and its dependencies.

Needs a database with the test schema and seed (docker/initdb): `FASTPG_DSN=... python
tests/smoke.py`.
"""

import asyncio
import os
from importlib.metadata import distribution
from pathlib import Path

import msgspec

import fastpg


class _Person(msgspec.Struct):
    id: int
    name: str


def _check_installed() -> None:
    # The wheel's, not a source tree's (which an editable install or a stray path would give).
    assert Path(fastpg.__file__).parent.parent.name == "site-packages", fastpg.__file__
    files = distribution("fastpg").files or []
    assert any(file.name.startswith("_native.") for file in files), files


async def _check_queries(dsn: str) -> None:
    async with await fastpg.connect(dsn) as conn:
        person = await conn.select_one("SELECT id, name FROM people WHERE id = $1", 1, into=_Person)
        assert person.id == 1, person
        count = await conn.select_one("SELECT count(*) FROM people")
        assert count == (100_000,), count
    pool = await fastpg.create_pool(dsn, max_size=2)
    try:
        query = "SELECT id, name FROM people WHERE id = $1"
        selects = [pool.select_one(query, i, into=_Person) for i in range(1, 9)]
        people = await asyncio.gather(*selects)
        assert [person.id for person in people] == list(range(1, 9)), people
    finally:
        await pool.close()


def main() -> None:
    _check_installed()
    asyncio.run(_check_queries(os.environ["FASTPG_DSN"]))


if __name__ == "__main__":
    main()
