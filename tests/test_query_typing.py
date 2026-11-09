"""Query strings are typed `LiteralString | Template`: a type checker flags SQL built at
runtime (an f-string, a joined str), while the query still runs as before.

Each `# pyright: ignore[...]` below must be needed: with the rule below, pyright reports an
ignore that isn't, so if a query method took any `str` again, `task typecheck` would fail.
"""

# pyright: reportUnnecessaryTypeIgnoreComment=true

from typing import LiteralString, cast

import fastpg


async def test_runtime_built_query_is_flagged_but_runs(dsn: str) -> None:
    n = 1
    query = f"SELECT {n}"
    async with await fastpg.connect(dsn) as conn:
        assert await conn.select_one(query) == (1,)  # pyright: ignore[reportArgumentType]
        assert await conn.select_many(query) == [(1,)]  # pyright: ignore[reportArgumentType]
        assert await conn.execute(query) == 1  # pyright: ignore[reportArgumentType]
        await conn.execute_script(query)  # pyright: ignore[reportArgumentType]
        assert await conn.execute_many(query, [()]) == 1  # pyright: ignore[reportArgumentType]
        async with conn.transaction() as tx:
            assert await tx.select_one(query) == (1,)  # pyright: ignore[reportArgumentType]
        # Deliberately dynamic SQL: cast() says so, and is not flagged.
        assert await conn.select_one(cast("LiteralString", query)) == (1,)
    async with await fastpg.create_pool(dsn, max_size=1) as pool:
        assert await pool.select_one(query) == (1,)  # pyright: ignore[reportArgumentType]
        assert await pool.execute_many(query, [()]) == 1  # pyright: ignore[reportArgumentType]
