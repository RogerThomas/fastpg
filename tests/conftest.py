"""Shared fixtures.

With ``FASTPG_DSN`` set, the tests use that server and start nothing; the TLS-server tests
then need ``FASTPG_TLS_DSN`` and ``FASTPG_TLS_CA`` too, and are skipped without them. With
``FASTPG_DSN`` unset, each server is started on first use in a throwaway container, set up
like its compose service, and removed at the end of the session.
"""

import asyncio
import os
import secrets
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import pytest
from faults import DisposableServer, FaultProxy, disposable_server, with_params
from testcontainers.core.container import DockerContainer

import fastpg

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Generator, Iterator


def _postgres_container(password: str) -> DockerContainer:
    """The compose services' image and user, data on tmpfs, on a host port Docker picks."""
    return (
        DockerContainer("postgres:18.6-trixie")
        .with_env("POSTGRES_USER", "fastpg")
        .with_env("POSTGRES_PASSWORD", password)
        .with_env("POSTGRES_DB", "fastpg")
        .with_exposed_ports(5432)
        .with_tmpfs_mount("/var/lib/postgresql", "size=1g")
    )


def _plain_container(docker_dir: Path, password: str) -> DockerContainer:
    """The compose `postgres` service, with its server flags (some tests depend on them, e.g.
    on work_mem); keep the two in step."""
    flags = [
        "max_connections=1000",
        "superuser_reserved_connections=5",
        "shared_buffers=2GB",
        "effective_cache_size=6GB",
        "work_mem=32MB",
        "maintenance_work_mem=512MB",
        "max_worker_processes=16",
        "max_parallel_workers=8",
        "jit=off",
        "fsync=off",
        "synchronous_commit=off",
        "full_page_writes=off",
        "wal_level=minimal",
        "max_wal_senders=0",
        "max_wal_size=4GB",
    ]
    return (
        _postgres_container(password)
        .with_command(["postgres", *(arg for flag in flags for arg in ("-c", flag))])
        .with_kwargs(shm_size="2g")
        .with_volume_mapping(docker_dir / "initdb", "/docker-entrypoint-initdb.d")
    )


def _tls_container(docker_dir: Path, certs: Path, password: str) -> DockerContainer:
    """The compose `postgres-tls` service, generating its certificates into `certs`."""
    return (
        _postgres_container(password)
        .with_kwargs(entrypoint=["/tls/entrypoint.sh"])
        .with_volume_mapping(docker_dir / "tls", "/tls")
        .with_volume_mapping(docker_dir / "tls" / "initdb", "/docker-entrypoint-initdb.d")
        .with_volume_mapping(certs, "/certs", "rw")
    )


async def _query(dsn: str) -> None:
    async with await fastpg.connect(dsn) as conn:
        await conn.select_one("SELECT 1")


async def _wait_until_ready(dsn: str) -> None:
    # The image's init-time server only listens on the unix socket, so the first query over
    # TCP that succeeds is answered by the final server, after the init scripts have run.
    async with asyncio.timeout(60):
        while True:
            try:
                await _query(dsn)
            except fastpg.Error:
                await asyncio.sleep(0.1)
            else:
                return


@contextmanager
def _running(container: DockerContainer, password: str) -> Generator[str]:
    """Start `container` (whose `fastpg` role has `password`); yield its DSN once it answers a
    query; remove it."""
    with container:
        host, port = container.get_container_host_ip(), container.get_exposed_port(5432)
        dsn = f"postgresql://fastpg:{password}@{host}:{port}/fastpg"
        try:
            asyncio.run(_wait_until_ready(dsn))
        except TimeoutError as error:
            error.add_note(b"".join(container.get_logs()).decode())
            raise
        yield dsn


@pytest.fixture(scope="session")
def docker_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "docker"


@pytest.fixture(scope="session")
def container_password() -> str:
    # Random per session: testcontainers publishes ports on all interfaces, not 127.0.0.1 only.
    return secrets.token_urlsafe(24)


@pytest.fixture(scope="session")
def dsn(docker_dir: Path, container_password: str) -> Iterator[str]:
    """``FASTPG_DSN``, or a throwaway server like the compose `postgres` service."""
    from_env = os.environ.get("FASTPG_DSN")
    if from_env is not None:
        yield from_env
        return
    with _running(_plain_container(docker_dir, container_password), container_password) as started:
        yield started


@pytest.fixture(scope="session")
def tls_server(
    docker_dir: Path,
    container_password: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, Path]]:
    """The TLS-only server's DSN and CA certificate (client certificate and key next to it):
    ``FASTPG_TLS_DSN`` and ``FASTPG_TLS_CA``, or a throwaway server like the compose
    `postgres-tls` service."""
    from_env, ca = os.environ.get("FASTPG_TLS_DSN"), os.environ.get("FASTPG_TLS_CA")
    if from_env is not None and ca is not None:
        yield from_env, Path(ca)
        return
    if "FASTPG_DSN" in os.environ:
        pytest.skip("FASTPG_DSN is set, but FASTPG_TLS_DSN and FASTPG_TLS_CA are not")
    certs = tmp_path_factory.mktemp("tls-certs")
    with _running(
        _tls_container(docker_dir, certs, container_password),
        container_password,
    ) as started:
        yield started, certs / "ca.crt"


@pytest.fixture
async def conn(dsn: str) -> AsyncIterator[fastpg.Connection]:
    async with await fastpg.connect(dsn) as connection:
        yield connection


@pytest.fixture
async def fault_proxy(dsn: str) -> AsyncIterator[FaultProxy]:
    """A started `FaultProxy` to the test server (``fault_proxy.route(dsn)`` goes through it)."""
    target = urlsplit(dsn)
    proxy = FaultProxy((target.hostname or "localhost", target.port or 5432))
    await proxy.start()
    yield proxy
    await proxy.close()


@pytest.fixture
def proxied_dsn(dsn: str, fault_proxy: FaultProxy) -> str:
    """`dsn` through `fault_proxy`, without TLS (so the bytes on the wire are the protocol's)."""
    return with_params(fault_proxy.route(dsn), sslmode="disable")


@pytest.fixture
async def tls_fault_proxy(tls_server: tuple[str, Path]) -> AsyncIterator[FaultProxy]:
    """A started `FaultProxy` to the TLS-only server."""
    target = urlsplit(tls_server[0])
    proxy = FaultProxy((target.hostname or "localhost", target.port or 5432))
    await proxy.start()
    yield proxy
    await proxy.close()


@pytest.fixture
def tls_proxied_dsn(tls_server: tuple[str, Path], tls_fault_proxy: FaultProxy) -> str:
    """The TLS server's DSN through `tls_fault_proxy`, encrypted but unverified (the proxy's
    address isn't in the server's certificate)."""
    return with_params(tls_fault_proxy.route(tls_server[0]), sslmode="require")


@pytest.fixture(scope="session")
def edge_server(container_password: str) -> Iterator[DisposableServer]:
    """A server of the network tests' own, which they pause, kill and restart: never the
    shared one (``FASTPG_DSN`` or the `dsn` container)."""
    name = f"fastpg-edge-net-{secrets.token_hex(4)}"
    with disposable_server(container_password, name) as server:
        asyncio.run(server.ready(60))
        yield server


@pytest.fixture
async def edge(edge_server: DisposableServer) -> DisposableServer:
    """`edge_server`, running and answering (whatever an earlier test did to it)."""
    await edge_server.heal()
    return edge_server
