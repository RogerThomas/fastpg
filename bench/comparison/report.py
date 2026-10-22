"""Environment capture, aggregation over trials, and the JSON / Markdown outputs.

Per (workload, configuration), over the successful trials: throughput median with min-max
and interquartile range, and the median of each trial's latency percentiles. Ratios are of
medians: against the default ``asyncpg`` configuration, and fastpg against the best
non-fastpg configuration of that workload (the skeptic's comparison).
"""

import datetime as dt
import os
import platform
import shutil
import statistics
import subprocess
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, LiteralString
from urllib.parse import urlsplit, urlunsplit

import msgspec
from psycopg import AsyncConnection

from .runner import Run
from .workloads import OpenLoop

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .adapters import Config
    from .runner import Plan
    from .workloads import Workload


class Environment(msgspec.Struct, kw_only=True):
    started_at: dt.datetime
    git_sha: str
    git_dirty: bool | None
    cpu_model: str
    logical_cpus: int | None
    usable_cpus: int | None
    memory_gb: float | None
    os: str
    python: str
    event_loop: str
    postgres: str
    postgres_settings: dict[str, str]
    packages: dict[str, str]
    ci: dict[str, str]


class Spread(msgspec.Struct, kw_only=True):
    median: float
    min: float
    max: float
    q1: float
    q3: float


class Aggregate(msgspec.Struct, kw_only=True, omit_defaults=True):
    workload: str
    config: str
    trials: int = 0
    failed: int = 0
    errors: list[str] = msgspec.field(default_factory=list[str])
    ops_per_s: Spread | None = None
    rows_per_s: float | None = None
    p50_us: float | None = None
    p90_us: float | None = None
    p99_us: float | None = None
    p999_us: float | None = None
    cpu_us_per_op: float | None = None
    vs_asyncpg: float | None = None
    # open loop
    achieved_rps: float | None = None
    service_p99_us: float | None = None
    loop_busy_pct: float | None = None
    overloaded_trials: int = 0


class Settings(msgspec.Struct, kw_only=True):
    """The run's knobs, with the DSN's password redacted."""

    dsn: str
    trials: int
    duration_s: float
    warmup_s: float
    pool_size: int
    seed: int
    max_inflight: int
    workloads: list[str]
    configs: list[str]


class WorkloadInfo(msgspec.Struct, kw_only=True):
    name: str
    title: str
    why: str
    category: str
    concurrency: int
    rows_per_op: int
    typed: bool | None
    skipped: dict[str, str]
    resets_session: bool = False


class ConfigInfo(msgspec.Struct, kw_only=True):
    name: str
    package: str
    version: str
    notes: str
    resets_session: bool = False


class Report(msgspec.Struct, kw_only=True):
    environment: Environment
    settings: Settings
    workloads: list[WorkloadInfo]
    configs: list[ConfigInfo]
    aggregates: list[Aggregate]
    runs: list[Run]

    @property
    def failures(self) -> list[Run]:
        return [r for r in self.runs if r.error is not None]


# ----------------------------------------------------------------------------- environment


class Probe:
    """Best-effort facts about the machine; never fails the run."""

    PACKAGES: ClassVar[tuple[str, ...]] = (
        "fastpg",
        "asyncpg",
        "psycopg",
        "psycopg-binary",
        "psycopg-pool",
        "psqlpy",
        "msgspec",
        "uvloop",
    )
    PG_SETTINGS: ClassVar[tuple[str, ...]] = (
        "server_version",
        "max_connections",
        "shared_buffers",
        "synchronous_commit",
        "fsync",
        "jit",
    )
    CAN_CHECKPOINT: ClassVar[LiteralString] = (
        "SELECT rolsuper OR pg_has_role(current_user, 'pg_checkpoint', 'MEMBER')"
        " FROM pg_roles WHERE rolname = current_user"
    )
    CI_VARS: ClassVar[tuple[str, ...]] = (
        "GITHUB_REPOSITORY",
        "GITHUB_REF_NAME",
        "GITHUB_RUN_ID",
        "GITHUB_EVENT_NAME",
        "RUNNER_OS",
        "RUNNER_ARCH",
        "ImageOS",
        "ImageVersion",
    )

    @staticmethod
    def _command(*args: str) -> str | None:
        exe = shutil.which(args[0])
        if exe is None:
            return None
        try:
            done = subprocess.run(  # noqa: S603 - fixed argv, resolved executable
                [exe, *args[1:]],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
        except OSError, subprocess.SubprocessError:
            return None
        return done.stdout.strip()

    @staticmethod
    def _proc_field(path: str, key: str) -> str | None:
        try:
            lines = Path(path).read_text().splitlines()
        except OSError:
            return None
        for line in lines:
            name, _, value = line.partition(":")
            if name.strip() == key:
                return value.strip()
        return None

    @classmethod
    def _cpu_model(cls) -> str:
        model = cls._proc_field("/proc/cpuinfo", "model name") or cls._command(
            "sysctl",
            "-n",
            "machdep.cpu.brand_string",
        )
        return model or platform.processor() or "unknown"

    @classmethod
    def _memory_gb(cls) -> float | None:
        if (kib := cls._proc_field("/proc/meminfo", "MemTotal")) is not None:
            return round(int(kib.split()[0]) / 2**20, 1)
        if (raw := cls._command("sysctl", "-n", "hw.memsize")) is not None:
            return round(int(raw) / 2**30, 1)
        return None

    @classmethod
    def _git(cls) -> tuple[str, bool | None]:
        repo = str(Path(__file__).resolve().parents[2])
        sha = cls._command("git", "-C", repo, "rev-parse", "HEAD")
        if sha is None:
            return os.environ.get("GITHUB_SHA", "unknown"), None
        status = cls._command("git", "-C", repo, "status", "--porcelain", "--untracked-files=no")
        return sha, None if status is None else bool(status)

    @classmethod
    def _packages(cls) -> dict[str, str]:
        versions: dict[str, str] = {}
        for package in cls.PACKAGES:
            try:
                versions[package] = metadata.version(package)
            except metadata.PackageNotFoundError:
                versions[package] = "not installed"
        return versions

    @classmethod
    async def _postgres(cls, dsn: str) -> tuple[str, dict[str, str]]:
        async with await AsyncConnection.connect(dsn, autocommit=True) as conn:
            cur = await conn.execute("SELECT version()")
            row = await cur.fetchone()
            version = str(row[0]) if row else "unknown"
            settings: dict[str, str] = {}
            for name in cls.PG_SETTINGS:
                cur = await conn.execute("SELECT current_setting(%s)", (name,))
                setting = await cur.fetchone()
                settings[name] = str(setting[0]) if setting else "unknown"
            cur = await conn.execute(cls.CAN_CHECKPOINT)
            allowed = await cur.fetchone()
            settings["checkpoint_between_runs"] = "yes" if allowed and allowed[0] else "no"
        return version, settings

    @classmethod
    async def environment(cls, dsn: str, event_loop: str) -> Environment:
        sha, dirty = cls._git()
        postgres, settings = await cls._postgres(dsn)
        return Environment(
            started_at=dt.datetime.now(dt.UTC),
            git_sha=sha,
            git_dirty=dirty,
            cpu_model=cls._cpu_model(),
            logical_cpus=os.cpu_count(),
            usable_cpus=os.process_cpu_count(),
            memory_gb=cls._memory_gb(),
            os=platform.platform(),
            python=f"{platform.python_implementation()} {platform.python_version()}",
            event_loop=event_loop,
            postgres=postgres,
            postgres_settings=settings,
            packages=cls._packages(),
            ci={k: os.environ[k] for k in cls.CI_VARS if k in os.environ},
        )


def redact(dsn: str) -> str:
    url = urlsplit(dsn)
    if url.password is None:
        return dsn
    return urlunsplit(url._replace(netloc=url.netloc.replace(f":{url.password}@", ":***@", 1)))


# ----------------------------------------------------------------------------- aggregation


def _spread(values: list[float]) -> Spread:
    if len(values) > 1:
        q1, _, q3 = statistics.quantiles(values, n=4, method="inclusive")
    else:
        q1 = q3 = values[0]
    return Spread(
        median=round(statistics.median(values), 1),
        min=round(min(values), 1),
        max=round(max(values), 1),
        q1=round(q1, 1),
        q3=round(q3, 1),
    )


def _median(values: Sequence[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return round(statistics.median(present), 2) if present else None


def _aggregate(workload: str, config: str, runs: list[Run]) -> Aggregate:
    ok = [r for r in runs if r.error is None]
    failed = [r for r in runs if r.error is not None]
    agg = Aggregate(
        workload=workload,
        config=config,
        trials=len(ok),
        failed=len(failed),
        errors=[f"trial {r.trial + 1}: {r.error_kind}: {r.error}" for r in failed],
    )
    if not ok:
        return agg
    throughput = [r.ops_per_s for r in ok if r.ops_per_s is not None]
    return msgspec.structs.replace(
        agg,
        ops_per_s=_spread(throughput) if throughput else None,
        rows_per_s=_median([r.rows_per_s for r in ok]),
        p50_us=_median([r.p50_us for r in ok]),
        p90_us=_median([r.p90_us for r in ok]),
        p99_us=_median([r.p99_us for r in ok]),
        p999_us=_median([r.p999_us for r in ok]),
        cpu_us_per_op=_median([r.cpu_us_per_op for r in ok]),
        achieved_rps=_median([r.achieved_rps for r in ok]),
        service_p99_us=_median([r.service_p99_us for r in ok]),
        loop_busy_pct=_median([r.loop_busy_pct for r in ok]),
        overloaded_trials=sum(1 for r in ok if r.overloaded),
    )


def aggregate(
    runs: Sequence[Run],
    workloads: Sequence[str],
    configs: Sequence[str],
) -> list[Aggregate]:
    """One Aggregate per (workload, config) that ran, with ``vs_asyncpg`` filled in."""
    out: list[Aggregate] = []
    for workload in workloads:
        mine = {c: [r for r in runs if r.workload == workload and r.config == c] for c in configs}
        aggs = {c: _aggregate(workload, c, rs) for c, rs in mine.items() if rs}
        base = aggs["asyncpg"].ops_per_s if "asyncpg" in aggs else None
        for agg in aggs.values():
            if base is not None and base.median > 0 and agg.ops_per_s is not None:
                agg.vs_asyncpg = round(agg.ops_per_s.median / base.median, 2)
            out.append(agg)
    return out


# -------------------------------------------------------------------------------- markdown


class Markdown:
    """The run as Markdown: verdict, environment, headline tables, then the details."""

    NOTE: ClassVar[str] = (
        "> Shared CI runners are noisy VMs, with PostgreSQL on the same machine as the "
        "client: absolute numbers are indicative only. Compare drivers **within** a run "
        "(ratios), not numbers across runs. Methodology, fairness choices and limitations: "
    )

    def __init__(self, report: Report) -> None:
        self._report = report
        self._configs = [c.name for c in report.configs]
        self._resetting = {c.name for c in report.configs if c.resets_session}
        self._workloads = {w.name: w for w in report.workloads}
        self._aggs = {(a.workload, a.config): a for a in report.aggregates}

    @staticmethod
    def _us(value: float | None) -> str:
        if value is None:
            return "-"
        if value >= 10_000:
            return f"{value / 1e3:,.1f} ms"
        return f"{value:,.0f} µs" if value >= 100 else f"{value:,.1f} µs"

    @staticmethod
    def _table(headers: Sequence[str], rows: Sequence[Sequence[str]], left: int = 1) -> list[str]:
        align = ["---" if i < left else "---:" for i in range(len(headers))]
        lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(align) + "|"]
        lines += ["| " + " | ".join(row) + " |" for row in rows]
        return [*lines, ""]

    def _note(self) -> str:
        env = self._report.environment
        if "GITHUB_REPOSITORY" in env.ci:
            repo = env.ci["GITHUB_REPOSITORY"]
            url = f"https://github.com/{repo}/blob/{env.git_sha}/docs/benchmarks.md"
            return f"{self.NOTE}[docs/benchmarks.md]({url})."
        return f"{self.NOTE}`docs/benchmarks.md`."

    def _verdict(self) -> list[str]:
        failures = self._report.failures
        total = len(self._report.runs)
        if not failures:
            return [
                (
                    f"**Correctness: PASSED.** All {total} runs returned (or wrote) exactly the "
                    "expected data."
                ),
                "",
            ]
        lines = [f"**Correctness: FAILED.** {len(failures)} of {total} runs failed:", ""]
        lines += [
            f"* `{r.workload}` / `{r.config}` (trial {r.trial + 1}): {r.error_kind}: {r.error}"
            for r in failures
        ]
        return [*lines, ""]

    def _environment(self) -> list[str]:
        env = self._report.environment
        s = self._report.settings
        dirty = " (uncommitted changes)" if env.git_dirty else ""
        cpus = f"{env.logical_cpus} logical, {env.usable_cpus} usable"
        rows = [
            ["git", f"`{env.git_sha[:12]}`{dirty}"],
            ["CPU", f"{env.cpu_model} ({cpus})"],
            ["memory", f"{env.memory_gb} GiB"],
            ["OS", env.os],
            ["Python", f"{env.python}, event loop: {env.event_loop}"],
            ["PostgreSQL", env.postgres.split(",")[0]],
            ["server settings", ", ".join(f"{k}={v}" for k, v in env.postgres_settings.items())],
            [
                "run",
                (
                    f"{s.trials} trials x {s.duration_s:g}s measured after "
                    f"{s.warmup_s:g}s warm-up, pool size {s.pool_size}, seed {s.seed}"
                ),
            ],
        ]
        if env.ci:
            rows.append(["CI", ", ".join(f"{k}={v}" for k, v in env.ci.items())])
        return ["### Environment", "", *self._table(["", ""], rows, left=2)]

    def _configs_table(self) -> list[str]:
        rows = [[f"`{c.name}`", f"{c.package} {c.version}", c.notes] for c in self._report.configs]
        return [
            "### Driver configurations",
            "",
            *self._table(["name", "version", "setup"], rows, 3),
        ]

    def _cell(self, workload: str, config: str) -> str:
        agg = self._aggs.get((workload, config))
        if agg is None:
            return "n/a"
        if agg.failed:
            return "**FAILED**"
        if agg.ops_per_s is None:
            return "-"
        ratio = f" ({agg.vs_asyncpg:.2f}x)" if agg.vs_asyncpg is not None else ""
        return f"{agg.ops_per_s.median:,.0f}{ratio}"

    def _fastpg_vs_best(self, workload: str) -> str:
        mine = self._aggs.get((workload, "fastpg"))
        others = [
            (a.ops_per_s.median, a.config)
            for c in self._configs
            if not c.startswith("fastpg")
            and (a := self._aggs.get((workload, c))) is not None
            and a.ops_per_s is not None
            and not a.failed
        ]
        if mine is None or mine.ops_per_s is None or mine.failed or not others:
            return "-"
        best, name = max(others)
        overall = f"{mine.ops_per_s.median / best:.2f}x {name}"
        info = self._workloads.get(workload)
        if info is None or not info.resets_session:
            return overall
        # fastpg resets the session here: like for like is a configuration that resets too.
        like = [(ops, c) for ops, c in others if c in self._resetting]
        if not like or max(like)[1] == name:
            return overall
        like_best, like_name = max(like)
        like_ratio = mine.ops_per_s.median / like_best
        return f"{like_ratio:.2f}x {like_name} (both reset the session); {overall} (no reset)"

    def _throughput(self) -> list[str]:
        closed = [w for w in self._report.workloads if w.category != "open-loop"]
        rows = [
            [
                f"`{w.name}`",
                *(self._cell(w.name, c) for c in self._configs),
                self._fastpg_vs_best(w.name),
            ]
            for w in closed
        ]
        headers = ["workload", *(f"`{c}`" for c in self._configs), "fastpg vs best other"]
        return [
            (
                f"### Throughput: median ops/s over {self._report.settings.trials} trials "
                "(ratio vs `asyncpg`)"
            ),
            "",
            (
                "Higher is better. One op is one query, transaction or batch (see the details). "
                "*n/a*: not run for that configuration (same code path as `fastpg`). Where "
                "fastpg resets the session when the connection goes back (transactions), it's "
                "compared first with the configurations that reset too: only `asyncpg`'s "
                "default does; `asyncpg-noreset`'s, psycopg's and psqlpy's pools don't."
            ),
            "",
            *self._table(headers, rows),
        ]

    def _open_loop(self) -> list[str]:
        rows: list[list[str]] = []
        for w in self._report.workloads:
            if w.category != "open-loop":
                continue
            for c in self._configs:
                agg = self._aggs.get((w.name, c))
                if agg is None:
                    continue
                if agg.failed or agg.trials == 0:
                    rows.append([f"`{w.name}`", f"`{c}`", "**FAILED**", *["-"] * 7])
                    continue
                over = f"{agg.overloaded_trials}/{agg.trials}" if agg.overloaded_trials else "no"
                rows.append(
                    [
                        f"`{w.name}`",
                        f"`{c}`",
                        f"{agg.achieved_rps or 0:,.0f}",
                        self._us(agg.p50_us),
                        self._us(agg.p90_us),
                        self._us(agg.p99_us),
                        self._us(agg.p999_us),
                        self._us(agg.service_p99_us),
                        f"{agg.loop_busy_pct or 0:.0f}%",
                        over,
                    ],
                )
        if not rows:
            return []
        headers = (
            *("rate", "config", "achieved/s", "p50", "p90", "p99", "p99.9"),
            *("service p99", "loop busy", "overloaded"),
        )
        return [
            "### Open loop: latency at equal load (median over trials)",
            "",
            (
                "Lower is better. Response time counts from each request's *scheduled* arrival "
                "(Poisson); *service p99* is dispatch to result; *loop busy* is the event loop "
                "thread's CPU share (headroom left for the application)."
            ),
            "",
            *self._table(headers, rows, left=2),
        ]

    def _details(self) -> list[str]:
        lines = ["### Details per workload", ""]
        headers = (
            *("config", "ops/s median", "min-max", "IQR", "vs asyncpg"),
            *("rows/s", "p50", "p90", "p99", "CPU/op"),
        )
        for w in self._report.workloads:
            if w.category == "open-loop":
                continue
            rows: list[list[str]] = []
            for c in self._configs:
                agg = self._aggs.get((w.name, c))
                if agg is None:
                    rows.append([f"`{c}`", f"n/a: {w.skipped.get(c, 'not run')}", *["-"] * 8])
                    continue
                if agg.failed or agg.ops_per_s is None:
                    rows.append([f"`{c}`", "**FAILED**", "; ".join(agg.errors), *["-"] * 7])
                    continue
                t = agg.ops_per_s
                rows.append(
                    [
                        f"`{c}`",
                        f"{t.median:,.0f}",
                        f"{t.min:,.0f}-{t.max:,.0f}",
                        f"{t.q1:,.0f}-{t.q3:,.0f}",
                        f"{agg.vs_asyncpg:.2f}x" if agg.vs_asyncpg is not None else "-",
                        f"{agg.rows_per_s or 0:,.0f}",
                        self._us(agg.p50_us),
                        self._us(agg.p90_us),
                        self._us(agg.p99_us),
                        self._us(agg.cpu_us_per_op),
                    ],
                )
            lines += [
                "<details>",
                f"<summary><code>{w.name}</code>: {w.title}</summary>",
                "",
                f"*Why:* {w.why}",
                "",
                *self._table(headers, rows),
                "</details>",
                "",
            ]
        lines += [
            (
                "*CPU/op* is the benchmark process's CPU time (every thread) per op; the server's "
                "isn't included. Latency percentiles are per op, median over trials."
            ),
            "",
        ]
        return lines

    def render(self) -> str:
        lines = [
            "## PostgreSQL driver benchmarks: fastpg vs asyncpg, psycopg, psqlpy",
            "",
            *self._verdict(),
            self._note(),
            "",
            *self._throughput(),
            *self._open_loop(),
            *self._environment(),
            *self._configs_table(),
            *self._details(),
        ]
        return "\n".join(lines)


def build(
    environment: Environment,
    plan: Plan,
    workloads: Sequence[Workload],
    configs: Sequence[Config],
    runs: list[Run],
    skipped: dict[str, dict[str, str]],
) -> Report:
    names = [w.name for w in workloads]
    config_names = [c.name for c in configs]
    return Report(
        environment=environment,
        settings=Settings(
            dsn=redact(plan.dsn),
            trials=plan.trials,
            duration_s=plan.duration_s,
            warmup_s=plan.warmup_s,
            pool_size=plan.pool_size,
            seed=plan.seed,
            max_inflight=plan.max_inflight,
            workloads=names,
            configs=config_names,
        ),
        workloads=[
            WorkloadInfo(
                name=w.name,
                title=w.title,
                why=w.why,
                category=w.category,
                concurrency=w.rate if isinstance(w, OpenLoop) else w.concurrency,
                rows_per_op=w.rows_per_op,
                typed=w.typed,
                skipped=skipped.get(w.name, {}),
                resets_session=w.resets_session,
            )
            for w in workloads
        ],
        configs=[
            ConfigInfo(
                name=c.name,
                package=c.package,
                version=environment.packages.get(c.package, "unknown"),
                notes=c.notes,
                resets_session=c.resets_session,
            )
            for c in configs
        ],
        aggregates=aggregate(runs, names, config_names),
        runs=runs,
    )


def write(report: Report, output_dir: Path) -> tuple[Path, Path]:
    """Write ``compare-<timestamp>.json`` and ``.md`` into ``output_dir``."""
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"compare-{report.environment.started_at:%Y%m%dT%H%M%SZ}"
    json_path = output_dir / f"{stem}.json"
    md_path = output_dir / f"{stem}.md"
    json_path.write_bytes(msgspec.json.format(msgspec.json.encode(report), indent=2) + b"\n")
    md_path.write_text(Markdown(report).render() + "\n")
    return json_path, md_path
