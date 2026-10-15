#!yeet
"""Pool vs single connection on a high-latency link, with per-call TCP state.

Reads the DSN from $NEON_DSN (never printed) unless --local is given.
"""

import asyncio
import contextvars
import datetime
import decimal
import os
import socket
import uuid
import statistics
import struct
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Any

import msgspec

import fastpg
from fastpg import _connection, _pool

LOCAL_DSN = "postgresql://fastpg:fastpg@127.0.0.1:55432/fastpg"
TCP_CONNECTION_INFO = 0x106
INFO = struct.Struct("=4B13I7Q")


class Result(msgspec.Struct):
    id: int
    run_id: str
    framework: str
    test: str
    attempt: int
    reqs_per_sec: float
    latency_avg_ms: float
    latency_p50_ms: float
    latency_p75_ms: float
    latency_p90_ms: float
    latency_p99_ms: float
    failed_rate: float
    total_requests: int
    score: float | None
    is_best: bool
    peak_rss_mb: float | None
    avg_rss_mb: float | None
    avg_cpu_pct: float | None
    peak_cpu_pct: float | None
    cpu_spark: str | None
    mem_spark: str | None


class Person(msgspec.Struct):
    id: int
    name: str
    age: int
    email: str | None
    score: float
    balance: decimal.Decimal
    active: bool
    external_id: uuid.UUID
    created_at: datetime.datetime
    birthday: datetime.date | None


@dataclass(slots=True)
class TCPInfo:
    rcv_wnd: int
    rcvbuf: int
    srtt_ms: int
    rxbytes: int
    rxpackets: int
    snd_cwnd: int


def tcp_info(sock: socket.socket) -> TCPInfo:
    raw = sock.getsockopt(socket.IPPROTO_TCP, TCP_CONNECTION_INFO, INFO.size)
    f = INFO.unpack(raw)
    # 0..3 bytes; 4 options 5 flags 6 rto 7 maxseg 8 ssthresh 9 cwnd 10 snd_wnd 11 sbbytes
    # 12 rcv_wnd 13 rttcur 14 srtt 15 rttvar 16 tfo; 17 txpackets 18 txbytes 19 txretx
    # 20 rxpackets 21 rxbytes 22 rxooo 23 txretxpackets
    return TCPInfo(
        rcv_wnd=f[12],
        rcvbuf=sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF),
        srtt_ms=f[14],
        rxbytes=f[21],
        rxpackets=f[20],
        snd_cwnd=f[9],
    )


@dataclass
class Tracer:
    """Maps each fastpg connection to its socket, and records which pool entry ran a call."""

    holder: contextvars.ContextVar[list[socket.socket]] = field(
        default_factory=partial(contextvars.ContextVar[list[socket.socket]], "holder"),
    )
    socks: dict[int, socket.socket] = field(default_factory=dict[int, socket.socket])
    used: list[tuple[int, int]] = field(default_factory=list[tuple[int, int]])
    orig_open: Any = None
    orig_connect: Any = None
    orig_call: Any = None

    async def open_socket(self, loop: Any, dsn: str) -> tuple[socket.socket, str]:
        sock, host = await self.orig_open(loop, dsn)
        self.holder.get().append(sock)
        return sock, host

    async def connect(self, dsn: str, **kw: Any) -> fastpg.Connection:
        self.holder.set([])
        conn = await self.orig_connect(dsn, **kw)
        self.socks[id(conn)] = self.holder.get()[0]
        return conn

    def call(self, pool: Any, entry: Any, *args: Any) -> Any:
        self.used.append((entry.slot, id(entry.conn)))
        return self.orig_call(pool, entry, *args)

    def install(self) -> None:
        self.orig_open = _connection._open_socket
        self.orig_connect = _connection.connect
        self.orig_call = _pool.Pool._call
        _connection._open_socket = self.open_socket
        _pool.connect = self.connect
        _pool.Pool._call = partial_method(self.call)

    def sock_of(self, conn: fastpg.Connection) -> socket.socket:
        return self.socks[id(conn)]


@dataclass
class partial_method:
    fn: Callable[..., Any]

    def __get__(self, obj: Any, objtype: Any = None) -> Callable[..., Any]:
        return partial(self.fn, obj)


@dataclass
class Sample:
    ms: float
    slot: int | None
    rx_kb: float
    rx_pkts: int
    rcv_wnd_kb: float
    rcvbuf_kb: float
    srtt_ms: int
    stmts: int


def snapshot(tracer: Tracer) -> dict[int, TCPInfo]:
    return {cid: tcp_info(sock) for cid, sock in tracer.socks.items() if sock.fileno() != -1}


async def timed(
    tracer: Tracer,
    op: Callable[[], Awaitable[object]],
    conn_of: Callable[[], fastpg.Connection | None],
) -> Sample:
    before_used = len(tracer.used)
    conn = conn_of()
    before = snapshot(tracer)
    t0 = time.perf_counter()
    await op()
    ms = (time.perf_counter() - t0) * 1000
    slot = None
    if conn is None:  # pool: the connection that ran it
        slot, cid = tracer.used[before_used]
    else:
        cid = id(conn)
    after = tcp_info(tracer.socks[cid])
    b = before[cid]
    return Sample(
        ms,
        slot,
        (after.rxbytes - b.rxbytes) / 1024,
        after.rxpackets - b.rxpackets,
        after.rcv_wnd / 1024,
        after.rcvbuf / 1024,
        after.srtt_ms,
        -1,
    )


def fmt(label: str, samples: list[Sample]) -> str:
    ms = [s.ms for s in samples]
    slots = "".join(str(s.slot) if s.slot is not None else "c" for s in samples)
    wnd = ",".join(f"{s.rcv_wnd_kb:.0f}" for s in samples)
    times = " ".join(f"{m:.1f}" for m in ms)
    return (
        f"{label:<28} median {statistics.median(ms):6.2f} ms  min {min(ms):6.2f}  slots {slots}\n"
        f"{'':28} times [{times}]\n"
        f"{'':28} rx {samples[-1].rx_kb:.0f} KB/{samples[-1].rx_pkts} pkts, srtt {samples[-1].srtt_ms} ms,"
        f" rcvbuf {samples[-1].rcvbuf_kb:.0f} KB, rcv_wnd KB after each [{wnd}]"
    )


async def series(
    tracer: Tracer,
    op: Callable[[], Awaitable[object]],
    conn_of: Callable[[], fastpg.Connection | None],
    runs: int,
    pause: float = 0.0,
) -> list[Sample]:
    out: list[Sample] = []
    for _ in range(runs):
        if pause:
            await asyncio.sleep(pause)
        out.append(await timed(tracer, op, conn_of))
    return out


def none_conn() -> None:
    return None


def const(conn: fastpg.Connection) -> fastpg.Connection:
    return conn


async def main(*, local: bool = False, runs: int = 9, rounds: int = 2, idle: bool = True) -> None:
    tracer = Tracer()
    tracer.install()
    if local:
        dsn = LOCAL_DSN
        query = "SELECT * FROM people ORDER BY id LIMIT 10000"
        into: type[Any] = Person
    else:
        dsn = os.environ["NEON_DSN"].strip().replace("&channel_binding=require", "")
        query = "SELECT * FROM results ORDER BY id"
        into = Result

    for rnd in range(rounds):
        print(f"=== round {rnd} ===")
        # 1. single connection, like the original script (SELECT 1 x10 first)
        conn = await tracer.connect(dsn)
        for _ in range(10):
            await conn.select_one("SELECT 1")
        s = await series(tracer, partial(conn.select_many, query, into=into), partial(const, conn), 1 + runs)
        print(fmt("conn (warmup incl.)", s))
        print(f"{'':28} cached statements: {conn.cached_statements}")
        if idle:
            for pause in (0.1, 0.25, 0.5, 1.0):
                s = await series(tracer, partial(conn.select_many, query, into=into), partial(const, conn), 5, pause)
                print(fmt(f"conn, idle {pause}s before", s))
        await conn.close()

        # 2. fresh single connection, no SELECT 1 warmup
        conn = await tracer.connect(dsn)
        s = await series(tracer, partial(conn.select_many, query, into=into), partial(const, conn), 1 + runs)
        print(fmt("fresh conn (warmup incl.)", s))
        await conn.close()

        # 3. pool 4/4
        tracer.used.clear()
        pool = await fastpg.create_pool(dsn, min_size=4, max_size=4)
        s = await series(tracer, partial(pool.select_many, query, into=into), none_conn, 1 + runs)
        print(fmt("pool 4/4 (warmup incl.)", s))
        print(f"{'':28} cached statements per conn: {[e.conn.cached_statements for e in pool._entries]}")
        await pool.close()

        # 4. pool 1/1
        tracer.used.clear()
        pool = await fastpg.create_pool(dsn, min_size=1, max_size=1)
        s = await series(tracer, partial(pool.select_many, query, into=into), none_conn, 1 + runs)
        print(fmt("pool 1/1 (warmup incl.)", s))
        await pool.close()
