#!yeet
import os
import statistics
import time
from collections.abc import Awaitable, Callable

import asyncpg
import msgspec

import fastpg


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


async def _median_ms(op: Callable[[], Awaitable[object]], runs: int = 9) -> float:
    await op()
    times: list[float] = []
    for _ in range(runs):
        t0 = time.perf_counter()
        await op()
        times.append(time.perf_counter() - t0)
    return statistics.median(times) * 1000


async def main() -> None:
    dsn = os.environ["NEON_DSN"].strip().replace("&channel_binding=require", "")
    query = "SELECT * FROM results ORDER BY id"
    async with await fastpg.connect(dsn) as conn:
        print(f"round trip (SELECT 1), fastpg conn:        {await _median_ms(lambda: conn.select_one('SELECT 1')):6.1f} ms")
        print(f"913 rows -> Structs, fastpg conn:           {await _median_ms(lambda: conn.select_many(query, into=Result)):6.1f} ms")
    async with await fastpg.create_pool(dsn, min_size=4, max_size=4) as pool:
        print(f"913 rows -> Structs, fastpg pool:           {await _median_ms(lambda: pool.select_many(query, into=Result)):6.1f} ms")
    aconn = await asyncpg.connect(dsn)
    try:
        print(f"round trip (SELECT 1), asyncpg conn:       {await _median_ms(lambda: aconn.fetchval('SELECT 1')):6.1f} ms")

        async def a_conn_fetch() -> list[Result]:
            return [Result(*r) for r in await aconn.fetch(query)]

        print(f"913 rows -> Structs, asyncpg conn:          {await _median_ms(a_conn_fetch):6.1f} ms")
    finally:
        await aconn.close()
    apool = await asyncpg.create_pool(dsn, min_size=4, max_size=4)
    try:

        async def a_pool_fetch() -> list[Result]:
            return [Result(*r) for r in await apool.fetch(query)]

        print(f"913 rows -> Structs, asyncpg pool:          {await _median_ms(a_pool_fetch):6.1f} ms")
    finally:
        await apool.close()
