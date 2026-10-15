#!yeet
"""Do separate connections to the same database differ systematically? Interleaved calls.

Peers are shown as labels (peer0, peer1, ...), not addresses. The DSN is never printed.
"""

import random
import statistics
from functools import partial

import fastpg
from ab import _setup, _time
from gap import Tracer


async def main(*, local: bool = False, conns: int = 8, iters: int = 30, seed: int = 2) -> None:
    tracer = Tracer()
    tracer.install()
    dsn, query, into = _setup(local)
    rng = random.Random(seed)
    peers: dict[str, str] = {}
    opened: list[tuple[fastpg.Connection, str]] = []
    for _ in range(conns):
        conn = await tracer.connect(dsn)
        ip = str(tracer.sock_of(conn).getpeername()[0])
        label = peers.setdefault(ip, f"peer{len(peers)}")
        for _ in range(5):
            await conn.select_one("SELECT 1")
        await conn.select_many(query, into=into)
        opened.append((conn, label))
    fetch: list[list[float]] = [[] for _ in opened]
    ping: list[list[float]] = [[] for _ in opened]
    order = list(range(len(opened)))
    for _ in range(iters):
        rng.shuffle(order)
        for i in order:
            conn = opened[i][0]
            fetch[i].append(await _time(partial(conn.select_many, query, into=into)))
            ping[i].append(await _time(partial(conn.select_one, "SELECT 1")))
    for i, (conn, label) in enumerate(opened):
        q = statistics.quantiles(fetch[i], n=4)
        print(
            f"conn {i} {label}: fetch median {statistics.median(fetch[i]):6.2f} (p25 {q[0]:6.2f} p75 {q[2]:6.2f})"
            f"  SELECT 1 median {statistics.median(ping[i]):6.2f} ms"
        )
        await conn.close()
    meds = [statistics.median(f) for f in fetch]
    print(f"spread of per-connection fetch medians: {min(meds):.2f}..{max(meds):.2f} ms")
