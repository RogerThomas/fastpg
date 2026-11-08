"""Several hosts in one DSN, taken as libpq takes them: host N goes with port N (a single
port goes with every host), they are tried in order, and ``target_session_attrs`` passes over
the sessions that don't match."""

import asyncio
from collections.abc import AsyncIterator
from urllib.parse import urlsplit, urlunsplit

import pytest
from faults import FaultProxy, with_params

import fastpg


@pytest.fixture
async def other_proxy(dsn: str) -> AsyncIterator[FaultProxy]:
    """A second started `FaultProxy` to the test server."""
    target = urlsplit(dsn)
    proxy = FaultProxy((target.hostname or "localhost", target.port or 5432))
    await proxy.start()
    yield proxy
    await proxy.close()


def at_hosts(dsn: str, hosts: str, ports: str, **params: str) -> str:
    """URL `dsn` with ``host`` and ``port`` lists instead of its host, without TLS."""
    parts = urlsplit(dsn)
    credentials = parts.netloc.rpartition("@")[0]
    url = urlunsplit(parts._replace(netloc=f"{credentials}@", query=""))
    return with_params(url, host=hosts, port=ports, sslmode="disable", **params)


async def test_each_host_goes_with_its_own_port(
    dsn: str,
    fault_proxy: FaultProxy,
    other_proxy: FaultProxy,
) -> None:
    """The first host (a unix socket directory that doesn't exist) fails; the second goes
    with the second port, never with the first one."""
    ports = f"{fault_proxy.port},{other_proxy.port}"
    async with await fastpg.connect(at_hosts(dsn, "/nonexistent-fastpg,127.0.0.1", ports)):
        pass
    assert fault_proxy.accepted == 0
    assert other_proxy.accepted == 1


async def test_one_port_goes_with_every_host(dsn: str, fault_proxy: FaultProxy) -> None:
    hosts = "/nonexistent-fastpg,127.0.0.1"
    async with await fastpg.connect(at_hosts(dsn, hosts, str(fault_proxy.port))) as conn:
        assert await conn.select_one("SELECT 1") == (1,)
    assert fault_proxy.accepted == 1


async def test_as_many_ports_as_hosts_or_one(dsn: str, fault_proxy: FaultProxy) -> None:
    ports = f"1,{fault_proxy.port}"
    with pytest.raises(fastpg.InterfaceError, match="could not match 2 port numbers to 3 hosts"):
        await fastpg.connect(at_hosts(dsn, "127.0.0.1,127.0.0.1,127.0.0.1", ports))
    assert fault_proxy.accepted == 0


async def test_a_refusing_host_falls_through_to_the_next(
    dsn: str,
    fault_proxy: FaultProxy,
) -> None:
    ports = f"1,{fault_proxy.port}"  # nothing listens on port 1
    async with await fastpg.connect(at_hosts(dsn, "127.0.0.1,127.0.0.1", ports)) as conn:
        assert await conn.select_one("SELECT 1") == (1,)


async def test_a_startup_timeout_falls_through_to_the_next_host(
    dsn: str,
    fault_proxy: FaultProxy,
    other_proxy: FaultProxy,
) -> None:
    """The first host accepts the TCP connection but never answers the startup."""
    fault_proxy.stall()
    ports = f"{fault_proxy.port},{other_proxy.port}"
    url = at_hosts(dsn, "127.0.0.1,127.0.0.1", ports, connect_timeout="2")
    async with await fastpg.connect(url) as conn:
        assert await conn.select_one("SELECT 1") == (1,)
    assert other_proxy.accepted == 1


@pytest.mark.parametrize(("attrs", "read_only"), [("read-write", "off"), ("read-only", "on")])
async def test_target_session_attrs_passes_over_a_session_that_does_not_match(
    dsn: str,
    fault_proxy: FaultProxy,
    other_proxy: FaultProxy,
    attrs: str,
    read_only: str,
) -> None:
    """The first host is the one that doesn't match: it is left for the second."""
    fault_proxy.read_only = read_only == "off"
    other_proxy.read_only = read_only == "on"
    ports = f"{fault_proxy.port},{other_proxy.port}"
    url = at_hosts(dsn, "127.0.0.1,127.0.0.1", ports, target_session_attrs=attrs)
    async with await fastpg.connect(url) as conn:
        assert await conn.select_one("SHOW transaction_read_only") == (read_only,)
    assert (fault_proxy.accepted, other_proxy.accepted) == (1, 1)
    async with asyncio.timeout(2):  # the session passed over is closed
        while fault_proxy.to_server.ended < 1:
            await asyncio.sleep(0.01)


async def test_target_session_attrs_with_no_session_matching_names_every_host(
    dsn: str,
    fault_proxy: FaultProxy,
    other_proxy: FaultProxy,
) -> None:
    fault_proxy.read_only = other_proxy.read_only = True
    ports = f"{fault_proxy.port},{other_proxy.port}"
    url = at_hosts(dsn, "127.0.0.1,127.0.0.1", ports, target_session_attrs="read-write")
    with pytest.raises(fastpg.InterfaceError) as exc:
        await fastpg.connect(url)
    message = str(exc.value)
    assert message.startswith("could not connect to server: ")
    for port in (fault_proxy.port, other_proxy.port):
        assert f"('127.0.0.1', {port}) failed: session is read-only" in message
    assert "target_session_attrs=read-write" in message
    async with asyncio.timeout(2):  # both sessions are closed
        while fault_proxy.to_server.ended + other_proxy.to_server.ended < 2:
            await asyncio.sleep(0.01)


async def test_target_session_attrs_any_takes_a_read_only_session(
    dsn: str,
    fault_proxy: FaultProxy,
) -> None:
    fault_proxy.read_only = True
    url = at_hosts(dsn, "127.0.0.1", str(fault_proxy.port), target_session_attrs="any")
    async with await fastpg.connect(url) as conn:
        assert await conn.select_one("SHOW transaction_read_only") == ("on",)


async def test_an_unknown_target_session_attrs_value_is_named(dsn: str) -> None:
    url = at_hosts(dsn, "127.0.0.1", "1", target_session_attrs="primary-ish")
    with pytest.raises(
        fastpg.InterfaceError,
        match='invalid target_session_attrs value: "primary-ish"',
    ):
        await fastpg.connect(url)
