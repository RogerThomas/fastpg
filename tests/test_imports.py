"""What importing and using fastpg imports: never pydantic, which only matters to applications
with pydantic models (who have imported it already; ADR 0003). Each case runs in a fresh
interpreter, since this one has imported pydantic already.
"""

import subprocess
import sys

import pytest

_IMPORT = "import fastpg"

_STRUCT_ROUND_TRIP = """
import asyncio
import sys

import msgspec

import fastpg


class Row(msgspec.Struct):
    id: int
    name: str


async def main(dsn: str) -> None:
    async with await fastpg.connect(dsn) as conn:
        row = await conn.select_one("SELECT 1 AS id, 'name' AS name", into=Row)
    assert row == Row(id=1, name="name")


asyncio.run(main(sys.argv[1]))
"""

_REPORT = """
import sys

print("pydantic" in sys.modules)
"""


def _pydantic_imported(code: str, dsn: str) -> str:
    """Whether pydantic is imported ("True" or "False") after running `code`, with `dsn` as its
    argument, in a fresh interpreter."""
    result = subprocess.run(  # noqa: S603 - our own interpreter and code
        [sys.executable, "-c", code + _REPORT, dsn],
        capture_output=True,
        encoding="utf-8",
        check=True,
    )
    return result.stdout.strip()


@pytest.mark.parametrize(
    "code",
    [
        pytest.param(_IMPORT, id="import"),
        pytest.param(_STRUCT_ROUND_TRIP, id="struct-round-trip"),
    ],
)
def test_pydantic_is_not_imported(code: str, dsn: str) -> None:
    assert _pydantic_imported(code, dsn) == "False"
