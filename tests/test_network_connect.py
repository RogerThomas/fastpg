"""Connecting when the database is down, refusing, or unreachable: for a connection, when a
pool starts, and when it grows."""

import pytest

import fastpg


@pytest.mark.parametrize(
    ("dsn", "message"),
    [
        (
            "postgresql://fastpg@127.0.0.1:1/fastpg?keepalives=1",
            'unsupported connection option "keepalives"',
        ),
        (
            "host=127.0.0.1 tcp_user_timeout=1000",
            'unsupported connection option "tcp_user_timeout"',
        ),
        ("host=127.0.0.1 port=x", "invalid connection string"),
    ],
)
async def test_invalid_connection_options_are_named(dsn: str, message: str) -> None:
    with pytest.raises(fastpg.InterfaceError, match=message):
        await fastpg.connect(dsn)
