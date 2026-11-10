#!yeet
"""The cross-driver comparison (``task bench-ci``) for a released fastpg, on AWS EC2 spot.

Runs it as .github/workflows/benchmarks.yml does with ``fastpg-version``: PostgreSQL 18.6 in
Docker on the same machine, the bench code cloned at ``--ref``, fastpg's wheel from PyPI. One
x86_64 (c8a.2xlarge) and one arm64 (m9g.2xlarge) run, each split by workload into pieces of
about ``--piece-minutes``. Every piece runs on a spot instance of its own, at most
``--max-instances`` at once, spread over the availability zones; a piece whose instance is
reclaimed starts again on a new one. Each piece's results are copied back when it finishes,
and each architecture's pieces are merged into one ``compare-<timestamp>.json`` and ``.md``
under ``--output-dir``. Everything the run creates in AWS is deleted when it ends, on Ctrl-C
too.

Needs the aws CLI with the ``fastpg-benchmarks`` profile, and ssh.

    uv run yeet ./bench/aws.py --fastpg-version 0.2.1
    uv run yeet ./bench/aws.py --fastpg-version 0.2.1 --arch arm64 -k 'point-*'
"""

import asyncio
import contextlib
import datetime as dt
import fnmatch
import logging
import math
import os
import re
import shlex
import signal
import tarfile
import tempfile
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Annotated, ClassVar, Literal

import msgspec
from comparison import report
from comparison.adapters import Configs
from comparison.runner import applies
from comparison.workloads import Workloads
from yeetr import Opt

logger = logging.getLogger("bench")

type Arch = Literal["x86_64", "arm64"]

PROFILE = "fastpg-benchmarks"
REGION = "eu-west-1"
VPC = "vpc-0fe7fa50b1daf8c5f"
SUBNETS = {
    "eu-west-1a": "subnet-005f27f772e3e43e5",
    "eu-west-1b": "subnet-068b3e3b4fa43bf22",
    "eu-west-1c": "subnet-0e77304307ac26256",
}
TAG = {"Key": "Project", "Value": "fastpg"}
RUN_TAG = "BenchRun"  # this run's id: teardown finds by it what a killed launch left behind
LIVE_STATES = "pending,running,shutting-down,stopping,stopped"


@dataclass(frozen=True, slots=True)
class Machine:
    instance_type: str
    label: str  # the report's CPU line


MACHINES: dict[Arch, Machine] = {
    "x86_64": Machine("c8a.2xlarge", "AWS c8a.2xlarge (AMD EPYC, 8 vCPU)"),
    "arm64": Machine("m9g.2xlarge", "AWS m9g.2xlarge (Graviton, 8 vCPU)"),
}

# Seconds per run on top of warm-up + measured time (pool, result checks, CHECKPOINT).
RUN_OVERHEAD_S = 1.0
POLL_S = 30.0
# Launch errors worth another availability zone (or, all failing, another round later).
CAPACITY_ERRORS = frozenset(
    {
        "InsufficientInstanceCapacity",
        "InsufficientCapacity",
        "InsufficientFreeAddressesInSubnet",
        "MaxSpotInstanceCountExceeded",
        "SpotMaxPriceTooLow",
        "Unsupported",
        "VcpuLimitExceeded",
    },
)
CAPACITY_ROUNDS = 15  # with the wait: give up after about 15 minutes
CAPACITY_WAIT_S = 60.0

# Runs on the instance (Amazon Linux 2023): `bash -s -- REF VERSION SHUTDOWN_MIN [bench-ci args]`.
# Sets up what benchmarks.yml does, starts `task bench-ci` as a systemd unit (a background job
# started over ssh can hold the session open) and returns; the exit status lands in
# ~/bench.exit. The job is a script file: systemd would expand `$` on a unit's command line.
# The shutdown is a dead man's switch: if this script's caller dies, the instance still
# terminates (shutdown behavior: terminate).
REMOTE = r"""
set -euo pipefail
ref=$1 version=$2 shutdown_min=$3
shift 3
sudo shutdown -h "+$shutdown_min" >/dev/null 2>&1
sudo dnf install -y -q docker git
sudo systemctl start docker
curl -LsSf https://astral.sh/uv/0.13.0/install.sh | UV_NO_MODIFY_PATH=1 sh
curl -fsSL https://taskfile.dev/install.sh | sudo sh -s -- -d -b /usr/local/bin v3.54.0
export PATH="$HOME/.local/bin:$PATH"
git clone -q --depth 1 --branch "$ref" https://github.com/RogerThomas/fastpg ~/fastpg
cd ~/fastpg
sudo docker run -d --name pg -p 55432:5432 \
    -e POSTGRES_USER=fastpg -e POSTGRES_PASSWORD=fastpg -e POSTGRES_DB=fastpg \
    postgres:18.6-trixie
until sudo docker exec pg pg_isready -h 127.0.0.1 -U fastpg -d fastpg >/dev/null 2>&1; do
    sleep 1
done
sudo docker exec pg psql -U fastpg -d fastpg -v ON_ERROR_STOP=1 \
    -c "ALTER SYSTEM SET synchronous_commit = off" -c "ALTER SYSTEM SET jit = off" \
    -c "SELECT pg_reload_conf()"
export UV_LOCKED=1
uv sync --all-groups --no-install-project
uv pip install "fastpg==$version"
export UV_NO_SYNC=1
uv run python -c \
    'import fastpg, importlib.metadata as m; print(m.version("fastpg"), fastpg.__file__)'
{
    echo "export HOME=$HOME PATH=$PATH UV_LOCKED=1 UV_NO_SYNC=1"
    echo "cd ~/fastpg"
    printf 'task bench-ci OUT=bench-results'
    printf ' %q' "$@"
    echo ' > ~/bench.log 2>&1 < /dev/null'
    echo 'echo $? > ~/bench.exit'
} > ~/bench.sh
sudo systemd-run --quiet --unit=fastpg-bench --uid=ec2-user bash "$HOME/bench.sh"
"""


class AwsError(Exception):
    def __init__(self, argv: list[str], stderr: str) -> None:
        super().__init__(f"aws {' '.join(argv)}: {stderr.strip()}")
        found = re.search(r"An error occurred \((\w+)\)", stderr)
        self.code = found.group(1) if found else None


class Reclaimed(Exception):  # noqa: N818 - an event, not an error
    """The spot instance went away (or is about to)."""


class NoCapacity(Exception):  # noqa: N818 - an event, not an error
    """No zone had spot capacity for the instance type, round after round."""


class Attempt(msgspec.Struct, kw_only=True):
    """One instance a piece ran on, for the run's record (instances.json)."""

    arch: str
    piece: int
    attempt: int
    instance_type: str
    capacity_errors: list[str] = msgspec.field(default_factory=list[str])
    instance_id: str | None = None
    zone: str | None = None
    launched_at: dt.datetime | None = None
    ended_at: dt.datetime | None = None
    outcome: str = "launching"


@dataclass(slots=True)
class Piece:
    arch: Arch
    index: int  # 1-based, within its architecture
    of: int
    workloads: list[str]
    estimate_s: float
    result_dir: Path

    @property
    def name(self) -> str:
        return f"{self.arch} {self.index}/{self.of}"


@dataclass(slots=True)
class Box:
    instance_id: str
    zone: str
    ip: str = ""
    spot_request: str | None = None


def _env() -> dict[str, str]:
    """The environment for aws: the profile's credentials, not the shell's."""
    drop = {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE"}
    return {k: v for k, v in os.environ.items() if k not in drop}


async def _exec(
    *argv: str,
    stdin: bytes | None = None,
    stdout: IO[bytes] | int = asyncio.subprocess.PIPE,
) -> tuple[int, bytes, bytes]:
    """Run ``argv`` in a session of its own (Ctrl-C is ours to handle); kill it if cancelled."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL if stdin is None else asyncio.subprocess.PIPE,
        stdout=stdout,
        stderr=asyncio.subprocess.PIPE,
        env=_env(),
        start_new_session=True,
    )
    try:
        out, err = await proc.communicate(stdin)
    except asyncio.CancelledError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise
    return proc.returncode or 0, out or b"", err


async def aws[T](*args: str, into: type[T]) -> T:
    argv = ["ec2", *args] if args[0] != "sts" else list(args)
    rc, out, err = await _exec(
        "aws",
        *("--profile", PROFILE, "--region", REGION, "--output", "json"),
        *argv,
    )
    if rc != 0:
        raise AwsError(list(args[:2]), err.decode(errors="replace"))
    return msgspec.json.decode(out or b"null", type=into)


def _tags(*resources: str, run_id: str, name: str | None = None) -> str:
    """``--tag-specifications``: Project=fastpg and this run's id on each resource type."""
    tags = [TAG, {"Key": RUN_TAG, "Value": run_id}]
    if name is not None:
        tags.append({"Key": "Name", "Value": name})
    return msgspec.json.encode([{"ResourceType": r, "Tags": tags} for r in resources]).decode()


def plan(
    archs: list[Arch],
    patterns: list[str] | None,
    trials: int,
    seconds_per_run: float,
    piece_minutes: float,
    output_dir: Path,
) -> list[Piece]:
    """Split the workloads into pieces of about ``piece_minutes``, in the suite's order."""
    workloads = Workloads.all([1_000, 4_000])
    if patterns:
        workloads = [w for w in workloads if any(fnmatch.fnmatch(w.name, p) for p in patterns)]
        if not workloads:
            msg = f"no workload matches {patterns}; see `uv run yeet ./bench/compare.py --list`"
            raise SystemExit(msg)
    configs = list(Configs.all())
    costs = [
        (w.name, sum(applies(w, c) for c in configs) * trials * seconds_per_run) for w in workloads
    ]
    total = sum(cost for _, cost in costs)
    count = min(len(costs), max(1, math.ceil(total / (piece_minutes * 60))))
    groups: list[list[tuple[str, float]]] = [[]]
    done = 0.0
    for name, cost in costs:
        # A new piece once this workload would end past the current piece's share.
        if groups[-1] and len(groups) < count and done + cost / 2 > total / count * len(groups):
            groups.append([])
        groups[-1].append((name, cost))
        done += cost
    # Interleaved, so both architectures start in the first wave.
    return [
        Piece(
            arch=arch,
            index=i + 1,
            of=len(groups),
            workloads=[name for name, _ in group],
            estimate_s=sum(cost for _, cost in group),
            result_dir=output_dir / arch / f"piece-{i + 1}",
        )
        for i, group in enumerate(groups)
        for arch in archs
    ]


@dataclass(slots=True)
class Fleet:
    """This run's AWS resources: created, driven, and always torn down."""

    run_id: str
    version: str
    ref: str
    bench_args: list[str]
    max_retries: int
    output_dir: Path
    ssh_dir: Path
    slots: asyncio.Semaphore
    key_name: str = ""
    group_id: str | None = None
    images: dict[Arch, str] = field(default_factory=dict[Arch, str])
    launched: set[str] = field(default_factory=set[str])
    attempts: list[Attempt] = field(default_factory=list[Attempt])
    zone_failures: dict[str, int] = field(default_factory=dict[str, int])
    failed: list[str] = field(default_factory=list[str])
    tearing_down: bool = False

    SSH_OPTIONS: ClassVar[tuple[str, ...]] = (
        *("-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no"),
        *("-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR"),
        *("-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15"),
        *("-o", "ServerAliveCountMax=4"),
    )

    # ------------------------------------------------------------------------- setup

    async def prepare(self, archs: list[Arch]) -> None:
        arn = await aws("sts", "get-caller-identity", "--query", "Arn", into=str)
        if not arn.endswith(f":user/{PROFILE}"):
            msg = f"aws identity is {arn}, not user/{PROFILE}: refusing to launch anything"
            raise SystemExit(msg)
        for arch in archs:
            self.images[arch] = await aws(
                *("describe-images", "--owners", "amazon", "--filters"),
                f"Name=name,Values=al2023-ami-2023.*-kernel-6.12-{arch}",
                "Name=state,Values=available",
                *("--query", "sort_by(Images, &CreationDate)[-1].ImageId"),
                into=str,
            )
        with urllib.request.urlopen("https://checkip.amazonaws.com", timeout=10) as reply:
            my_ip = reply.read().decode().strip()
        key = self.ssh_dir / "id_ed25519"
        rc, _, err = await _exec("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key))
        if rc != 0:
            raise RuntimeError(err.decode())
        self.key_name = f"{PROFILE}-{self.run_id}"
        await aws(
            *("import-key-pair", "--key-name", self.key_name),
            *("--public-key-material", f"fileb://{key}.pub"),
            *("--tag-specifications", _tags("key-pair", run_id=self.run_id)),
            into=object,
        )
        self.group_id = await aws(
            *("create-security-group", "--group-name", self.key_name, "--vpc-id", VPC),
            *("--description", f"fastpg bench-aws run {self.run_id}: ssh from {my_ip}"),
            *("--tag-specifications", _tags("security-group", run_id=self.run_id)),
            *("--query", "GroupId"),
            into=str,
        )
        await aws(
            *("authorize-security-group-ingress", "--group-id", self.group_id),
            *("--protocol", "tcp", "--port", "22", "--cidr", f"{my_ip}/32"),
            into=object,
        )
        logger.info(
            "run %s: key pair and security group %s (ssh from %s/32); AMIs %s",
            self.run_id,
            self.group_id,
            my_ip,
            self.images,
        )

    # ------------------------------------------------------------------------ pieces

    async def run(self, pieces: list[Piece]) -> None:
        async with asyncio.TaskGroup() as group:
            for piece in pieces:
                group.create_task(self._piece(piece))

    async def _piece(self, piece: Piece) -> None:
        for attempt in range(1, self.max_retries + 2):
            record = Attempt(
                arch=piece.arch,
                piece=piece.index,
                attempt=attempt,
                instance_type=MACHINES[piece.arch].instance_type,
            )
            self.attempts.append(record)
            async with self.slots:
                try:
                    box = await self._launch(piece, record)
                except NoCapacity as exc:
                    record.outcome = f"no capacity: {exc}"
                    self.failed.append(f"{piece.name}: {exc}")
                    return
                except asyncio.CancelledError:
                    record.outcome = "interrupted"
                    raise
                try:
                    record.outcome = await self._drive(piece, box, attempt)
                except Reclaimed as exc:
                    record.outcome = f"reclaimed: {exc}"
                    logger.warning("[%s] %s: %s", piece.name, box.instance_id, record.outcome)
                    continue
                except asyncio.CancelledError:
                    record.outcome = "interrupted"
                    raise
                except Exception as exc:
                    record.outcome = f"failed: {type(exc).__name__}: {exc}"
                    raise
                finally:
                    record.ended_at = dt.datetime.now(dt.UTC)
                    await self._terminate(box)
            return
        self.failed.append(f"{piece.name}: reclaimed {self.max_retries + 1} times, gave up")
        logger.error("[%s] reclaimed %d times: giving up", piece.name, self.max_retries + 1)

    async def _launch(self, piece: Piece, record: Attempt) -> Box:
        """A spot instance in the first zone with capacity, starting from a different zone
        per piece and leaving the zones that failed lately for last."""
        machine = MACHINES[piece.arch]
        names = list(SUBNETS)
        for round_ in range(1, CAPACITY_ROUNDS + 1):
            zones = sorted(
                names,
                key=lambda z: (
                    self.zone_failures.get(f"{machine.instance_type}/{z}", 0),
                    (names.index(z) - piece.index - (piece.arch == "arm64")) % len(names),
                ),
            )
            for zone in zones:
                try:
                    box = await self._run_instance(piece, zone)
                except AwsError as exc:
                    if exc.code not in CAPACITY_ERRORS:
                        raise
                    key = f"{machine.instance_type}/{zone}"
                    self.zone_failures[key] = self.zone_failures.get(key, 0) + 1
                    record.capacity_errors.append(f"{zone}: {exc.code}")
                    logger.warning(
                        "[%s] %s in %s: %s",
                        piece.name,
                        machine.instance_type,
                        zone,
                        exc.code,
                    )
                    continue
                record.instance_id, record.zone = box.instance_id, zone
                record.launched_at = dt.datetime.now(dt.UTC)
                record.outcome = "running"
                logger.info(
                    "[%s] launched %s (%s) in %s: %s, about %.0f min",
                    piece.name,
                    box.instance_id,
                    machine.instance_type,
                    zone,
                    " ".join(piece.workloads),
                    piece.estimate_s / 60,
                )
                return box
            logger.warning(
                "[%s] no %s capacity in any zone (round %d/%d)",
                piece.name,
                machine.instance_type,
                round_,
                CAPACITY_ROUNDS,
            )
            if round_ < CAPACITY_ROUNDS:
                await asyncio.sleep(CAPACITY_WAIT_S)
        msg = f"no {machine.instance_type} spot capacity in {CAPACITY_ROUNDS} rounds"
        logger.error("[%s] %s: giving up", piece.name, msg)
        raise NoCapacity(msg)

    async def _run_instance(self, piece: Piece, zone: str) -> Box:
        assert self.group_id is not None  # noqa: S101 - prepare() ran
        name = f"fastpg-bench-{self.run_id}-{piece.arch}-{piece.index}"
        interface = {
            "DeviceIndex": 0,
            "SubnetId": SUBNETS[zone],
            "Groups": [self.group_id],
            "AssociatePublicIpAddress": True,
        }
        market = {
            "MarketType": "spot",
            "SpotOptions": {
                "SpotInstanceType": "one-time",
                "InstanceInterruptionBehavior": "terminate",
            },
        }
        disk = {
            "DeviceName": "/dev/xvda",
            "Ebs": {"VolumeSize": 30, "VolumeType": "gp3", "DeleteOnTermination": True},
        }
        launched = await aws(
            *("run-instances", "--count", "1", "--image-id", self.images[piece.arch]),
            *("--instance-type", MACHINES[piece.arch].instance_type, "--key-name", self.key_name),
            *("--network-interfaces", msgspec.json.encode([interface]).decode()),
            *("--instance-market-options", msgspec.json.encode(market).decode()),
            *("--instance-initiated-shutdown-behavior", "terminate"),
            *("--block-device-mappings", msgspec.json.encode([disk]).decode()),
            *("--tag-specifications", _tags("instance", "volume", run_id=self.run_id, name=name)),
            *("--query", "Instances[0].[InstanceId, SpotInstanceRequestId]"),
            into=tuple[str, str | None],
        )
        self.launched.add(launched[0])
        return Box(instance_id=launched[0], zone=zone, spot_request=launched[1])

    async def _drive(self, piece: Piece, box: Box, attempt: int) -> str:
        """Set up, start, wait for and fetch one piece. Returns its outcome."""
        await self._wait_running(box)
        await self._wait_ssh(piece, box)
        piece.result_dir.mkdir(parents=True, exist_ok=True)
        shutdown_min = math.ceil(piece.estimate_s * 3 / 60) + 30
        args = [self.ref, self.version, str(shutdown_min), *self.bench_args, "--"]
        args += [arg for w in piece.workloads for arg in ("-k", w)]
        logger.info(
            "[%s] setting up (fastpg %s, bench code at %s)",
            piece.name,
            self.version,
            self.ref,
        )
        log = piece.result_dir / f"setup-{attempt}.log"
        rc, out, err = await self._ssh(
            box,
            f"bash -s -- {shlex.join(args)}",
            stdin=REMOTE.encode(),
        )
        log.write_bytes(out + err)
        if rc != 0:
            await self._check_alive(box)
            tail = (out + err).decode(errors="replace").strip().splitlines()[-15:]
            msg = f"[{piece.name}] setup failed (exit {rc}), see {log}:\n" + "\n".join(tail)
            raise RuntimeError(msg)
        installed = out.decode(errors="replace").strip().splitlines()
        logger.info("[%s] benchmarking fastpg %s", piece.name, installed[-1] if installed else "?")
        status = await self._wait_done(piece, box)
        await self._fetch(piece, box)
        if status != 0:
            self.failed.append(f"{piece.name}: task bench-ci exited {status}")
            logger.error(
                "[%s] task bench-ci exited %d (see %s)",
                piece.name,
                status,
                piece.result_dir,
            )
            return f"bench-ci exited {status}"
        logger.info("[%s] done, results in %s", piece.name, piece.result_dir)
        return "done"

    async def _ssh(
        self,
        box: Box,
        command: str,
        stdin: bytes | None = None,
        stdout: IO[bytes] | int = asyncio.subprocess.PIPE,
    ) -> tuple[int, bytes, bytes]:
        key = str(self.ssh_dir / "id_ed25519")
        return await _exec(
            *("ssh", "-i", key, *self.SSH_OPTIONS, f"ec2-user@{box.ip}", command),
            stdin=stdin,
            stdout=stdout,
        )

    async def _wait_running(self, box: Box) -> None:
        for _ in range(60):
            state, ip = await aws(
                *("describe-instances", "--instance-ids", box.instance_id),
                *("--query", "Reservations[0].Instances[0].[State.Name, PublicIpAddress]"),
                into=tuple[str, str | None],
            )
            if state == "running" and ip:
                box.ip = ip
                return
            if state != "pending":
                msg = f"instance {state} before it ran"
                raise Reclaimed(msg)
            await asyncio.sleep(5)
        msg = f"{box.instance_id} not running after 5 minutes"
        raise RuntimeError(msg)

    async def _wait_ssh(self, piece: Piece, box: Box) -> None:
        for _ in range(60):
            rc, _, _ = await self._ssh(box, "true")
            if rc == 0:
                return
            await self._check_alive(box)
            await asyncio.sleep(5)
        msg = f"[{piece.name}] no ssh to {box.ip} after 5 minutes"
        raise RuntimeError(msg)

    async def _check_alive(self, box: Box) -> None:
        """Raise Reclaimed if the instance is gone, or spot is about to take it."""
        state, reason = await aws(
            *("describe-instances", "--instance-ids", box.instance_id),
            *("--query", "Reservations[0].Instances[0].[State.Name, StateReason.Code]"),
            into=tuple[str, str | None],
        )
        if state != "running":
            msg = f"instance {state} ({reason})"
            raise Reclaimed(msg)
        if box.spot_request is None:
            return
        code = await aws(
            *("describe-spot-instance-requests", "--spot-instance-request-ids", box.spot_request),
            *("--query", "SpotInstanceRequests[0].Status.Code"),
            into=str,
        )
        if code.startswith(("instance-terminated", "instance-stopped", "marked-for")):
            msg = f"spot request status {code}"
            raise Reclaimed(msg)

    async def _wait_done(self, piece: Piece, box: Box) -> int:
        """The exit status of `task bench-ci`, polled; Reclaimed if the instance goes."""
        deadline = time.monotonic() + piece.estimate_s * 2 + 15 * 60
        started = time.monotonic()
        failures = 0
        while True:
            await asyncio.sleep(POLL_S)
            await self._check_alive(box)
            rc, out, _ = await self._ssh(box, "cat ~/bench.exit 2>/dev/null || true")
            if rc != 0:
                failures += 1
                if failures >= 10:
                    msg = f"[{piece.name}] ssh to {box.ip} failed 10 times in a row"
                    raise RuntimeError(msg)
                continue
            failures = 0
            if out.strip():
                return int(out)
            if time.monotonic() > deadline:
                minutes = (time.monotonic() - started) / 60
                msg = f"[{piece.name}] still running after {minutes:.0f} min"
                raise RuntimeError(msg)

    async def _fetch(self, piece: Piece, box: Box) -> None:
        archive = piece.result_dir / "results.tgz"
        for _ in range(3):
            with archive.open("wb") as out:
                rc, _, err = await self._ssh(
                    box,
                    "tar czf - -C ~ bench.log -C ~/fastpg bench-results",
                    stdout=out,
                )
            if rc == 0:
                break
            logger.warning("[%s] copying the results failed: %s", piece.name, err.decode().strip())
            await self._check_alive(box)
            await asyncio.sleep(5)
        else:
            msg = f"[{piece.name}] could not copy the results from {box.ip}"
            raise RuntimeError(msg)
        with tarfile.open(archive) as tar:
            tar.extractall(piece.result_dir, filter="data")

    async def _terminate(self, box: Box) -> None:
        with contextlib.suppress(AwsError):
            await aws("terminate-instances", "--instance-ids", box.instance_id, into=object)

    # ---------------------------------------------------------------------- teardown

    async def teardown(self) -> None:
        """Terminate every instance of this run and wait; delete its key pair and group."""
        self.tearing_down = True
        logger.info("run %s: tearing down", self.run_id)
        for _ in range(3):
            found = await self._live_instances(f"tag:{RUN_TAG}", self.run_id)
            ids = sorted(set(found) | self.launched)
            if not found:
                break
            logger.info("terminating %s", " ".join(ids))
            await aws("terminate-instances", "--instance-ids", *ids, into=object)
            await aws("wait", "instance-terminated", "--instance-ids", *ids, into=object)
        if self.group_id is not None:
            for _ in range(30):  # the instances' network interfaces take a while to go
                try:
                    await aws("delete-security-group", "--group-id", self.group_id, into=object)
                    break
                except AwsError as exc:
                    if exc.code != "DependencyViolation":
                        raise
                    await asyncio.sleep(10)
        if self.key_name:
            await aws("delete-key-pair", "--key-name", self.key_name, into=object)
        await self.verify()

    async def _live_instances(self, tag: str, value: str) -> list[str]:
        return await aws(
            "describe-instances",
            *(
                "--filters",
                f"Name={tag},Values={value}",
                f"Name=instance-state-name,Values={LIVE_STATES}",
            ),
            *("--query", "Reservations[].Instances[].InstanceId"),
            into=list[str],
        )

    async def verify(self) -> None:
        """Log what's left tagged Project=fastpg; complain if any of it is this run's."""
        instances = await aws(
            "describe-instances",
            *(
                "--filters",
                "Name=tag:Project,Values=fastpg",
                f"Name=instance-state-name,Values={LIVE_STATES}",
            ),
            *("--query", "Reservations[].Instances[].[InstanceId, State.Name, Tags]"),
            into=list[object],
        )
        volumes = await aws(
            *("describe-volumes", "--filters", "Name=tag:Project,Values=fastpg"),
            *("--query", "Volumes[].[VolumeId, State, Tags]"),
            into=list[object],
        )
        groups = await aws(
            *("describe-security-groups", "--filters", f"Name=tag:{RUN_TAG},Values={self.run_id}"),
            *("--query", "SecurityGroups[].GroupId"),
            into=list[str],
        )
        keys = await aws(
            *("describe-key-pairs", "--filters", f"Name=tag:{RUN_TAG},Values={self.run_id}"),
            *("--query", "KeyPairs[].KeyName"),
            into=list[str],
        )
        logger.info("non-terminated instances tagged Project=fastpg: %s", instances)
        logger.info("volumes tagged Project=fastpg: %s", volumes)
        logger.info("this run's security groups: %s, key pairs: %s", groups, keys)
        mine = [
            item
            for item in (*instances, *volumes)
            if self.run_id in msgspec.json.encode(item).decode()
        ]
        if mine or groups or keys:
            msg = f"run {self.run_id} left resources behind: {mine} {groups} {keys}"
            raise RuntimeError(msg)
        logger.info("run %s: nothing left running", self.run_id)

    # ------------------------------------------------------------------------ results

    def merge(self, pieces: list[Piece]) -> None:
        """Each architecture's pieces as one compare JSON and Markdown in its directory."""
        archs: list[Arch] = list(dict.fromkeys(p.arch for p in pieces))
        for arch in archs:
            paths = [
                path
                for p in pieces
                if p.arch == arch
                for path in sorted((p.result_dir / "bench-results").glob("compare-*.json"))
            ]
            if not paths:
                continue
            reports = [msgspec.json.decode(path.read_bytes(), type=report.Report) for path in paths]
            merged = report.merge(reports, machine=MACHINES[arch].label)
            json_path, md_path = report.write(merged, self.output_dir / arch)
            logger.info("%s: %d pieces merged into %s and %s", arch, len(paths), json_path, md_path)
            if len(paths) < sum(p.arch == arch for p in pieces):
                self.failed.append(f"{arch}: only {len(paths)} pieces have results")

    def write_record(self) -> None:
        path = self.output_dir / "instances.json"
        path.write_bytes(msgspec.json.format(msgspec.json.encode(self.attempts), indent=2) + b"\n")
        for a in self.attempts:
            logger.info(
                "%s piece %d, attempt %d: %s %s in %s, %s%s",
                a.arch,
                a.piece,
                a.attempt,
                a.instance_type,
                a.instance_id,
                a.zone,
                a.outcome,
                f" (capacity errors: {', '.join(a.capacity_errors)})" if a.capacity_errors else "",
            )


async def main(
    *,
    fastpg_version: Annotated[str, Opt(help="The fastpg release to benchmark, from PyPI")],
    ref: Annotated[
        str | None,
        Opt(help="Tag or branch of the bench code. Default: v<fastpg-version>"),
    ] = None,
    arch: Annotated[
        list[Arch] | None,
        Opt(alias="a", help="Architecture, repeatable. Default: x86_64 and arm64"),
    ] = None,
    trials: Annotated[int, Opt(alias="n", help="Trials")] = 3,
    duration: Annotated[float, Opt(alias="t", help="Measured seconds per run")] = 3.0,
    warmup: Annotated[float, Opt(alias="w", help="Warm-up seconds per run")] = 1.0,
    workloads: Annotated[
        list[str] | None,
        Opt(alias="k", help="Workload name/glob, repeatable. Default: all"),
    ] = None,
    piece_minutes: Annotated[float, Opt(help="Benchmark minutes per instance, about")] = 15.0,
    max_instances: Annotated[int, Opt(help="Instances at once (spot quota: 32 vCPUs)")] = 4,
    max_retries: Annotated[int, Opt(help="Relaunches per piece after a spot reclaim")] = 2,
    output_dir: Annotated[
        Path | None,
        Opt(alias="o", help="Default: bench/results/aws-<run id>"),
    ] = None,
) -> None:
    """Run `task bench-ci` for a fastpg release on EC2 spot instances, x86_64 and arm64."""
    archs: list[Arch] = list(dict.fromkeys(arch or ["x86_64", "arm64"]))
    run_id = dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M%S")
    out = output_dir or Path(__file__).parent / "results" / f"aws-{run_id}"
    seconds_per_run = warmup + duration + RUN_OVERHEAD_S
    pieces = plan(archs, workloads, trials, seconds_per_run, piece_minutes, out)
    for piece in pieces[:: len(archs)]:
        logger.info(
            "piece %d/%d, about %.0f min: %s",
            piece.index,
            piece.of,
            piece.estimate_s / 60,
            " ".join(piece.workloads),
        )
    out.mkdir(parents=True, exist_ok=True)
    task = asyncio.current_task()
    assert task is not None  # noqa: S101 - running in a task
    with tempfile.TemporaryDirectory(prefix="fastpg-bench-aws-") as ssh_dir:
        fleet = Fleet(
            run_id=run_id,
            version=fastpg_version,
            ref=ref or f"v{fastpg_version}",
            bench_args=[f"TRIALS={trials}", f"DURATION={duration:g}", f"WARMUP={warmup:g}"],
            max_retries=max_retries,
            output_dir=out,
            ssh_dir=Path(ssh_dir),
            slots=asyncio.Semaphore(max_instances),
        )

        def stop(name: str) -> None:
            if fleet.tearing_down:
                logger.warning("%s: already tearing down, please wait", name)
                return
            logger.warning("%s: stopping", name)
            fleet.tearing_down = True
            task.cancel()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop, sig.name)
        interrupted = False
        try:
            await fleet.prepare(archs)
            await fleet.run(pieces)
        except asyncio.CancelledError:
            task.uncancel()
            interrupted = True
        finally:
            fleet.tearing_down = True
            await fleet.teardown()
            fleet.write_record()
            fleet.merge(pieces)
    if interrupted:
        msg = f"interrupted; everything torn down. Partial results: {out}"
        raise SystemExit(msg)
    if fleet.failed:
        msg = "some pieces failed:\n" + "\n".join(fleet.failed)
        raise SystemExit(msg)
    logger.info("results: %s", out)
