import os
from typing import TYPE_CHECKING

import pytest

import fastpg

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@pytest.fixture
def dsn() -> str:
    return os.environ.get("FASTPG_DSN", "postgresql://fastpg:fastpg@127.0.0.1:55432/fastpg")


@pytest.fixture
async def conn(dsn: str) -> AsyncIterator[fastpg.Connection]:
    async with await fastpg.connect(dsn) as connection:
        yield connection
