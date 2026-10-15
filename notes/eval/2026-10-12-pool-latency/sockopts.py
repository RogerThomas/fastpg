#!yeet
"""Socket options of a connect() connection vs a pool's connections (no queries sent)."""

import socket

import fastpg
from ab import _setup
from gap import Tracer


def _opts(sock: socket.socket) -> str:
    nodelay = sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY)
    rcv = sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
    snd = sock.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF)
    keep = sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)
    return f"TCP_NODELAY={nodelay} SO_RCVBUF={rcv} SO_SNDBUF={snd} SO_KEEPALIVE={keep}"


async def main(*, local: bool = False) -> None:
    tracer = Tracer()
    tracer.install()
    dsn, _, _ = _setup(local)
    conn = await tracer.connect(dsn)
    print(f"connect():   {_opts(tracer.sock_of(conn))}")
    async with await fastpg.create_pool(dsn, min_size=4, max_size=4) as pool:
        for cid, sock in tracer.socks.items():
            if cid != id(conn):
                print(f"pool conn:   {_opts(sock)}")
        print(pool.stats())
    await conn.close()
