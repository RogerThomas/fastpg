import asyncio
import time
import timeit

import fastpg


def f() -> None:
    pass


class C:
    __slots__ = ("a",)

    def m(self) -> None:
        pass


async def main() -> None:
    conn = await fastpg.connect("postgresql://fastpg:fastpg@localhost:55432/fastpg")
    nat = conn._native
    c = C()
    c.a = 1
    d = {"x": 1}
    g = {
        "conn": conn,
        "nat": nat,
        "time": time,
        "f": f,
        "c": c,
        "d": d,
        "pc": time.perf_counter_ns,
    }
    for label, stmt in [
        ("empty loop", "pass"),
        ("py call f()", "f()"),
        ("py method c.m()", "c.m()"),
        ("slot attr c.a", "c.a"),
        ("dict get", "d.get('x')"),
        ("perf_counter_ns()", "pc()"),
        ("time.perf_counter_ns()", "time.perf_counter_ns()"),
        ("conn.closed (property->native)", "conn.closed"),
        ("native.closed getter", "nat.closed"),
        ("native.statement_cache_len()", "nat.statement_cache_len()"),
        ("tuple (a, b) build", "(c, d)"),
    ]:
        n = 2_000_000
        best = min(timeit.repeat(stmt, globals=g, number=n, repeat=5))
        print(f"{label:34s} {best / n * 1e9:6.1f} ns")
    await conn.close()


asyncio.run(main())
