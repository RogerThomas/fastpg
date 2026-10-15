"""Pipeline-earlier prototypes, from the installed (Router) pool.

h2: before taking an idle connection, join the least stalled busy one if its score is
    below 2x this statement's own expected cost (no reserve: at any load).
h2r: the same, but only once at most a quarter of max_size is left idle (a reserve).
"""

from pathlib import Path

V = Path(__file__).parent / "v"
SRC = Path("<repo>/src/fastpg/_pool.py")


def rep(s: str, a: str, b: str) -> str:
    assert s.count(a) == 1, a
    return s.replace(a, b)


cur = SRC.read_text()
(V / "router.py").write_text(cur)

OLD = """        idle = self._idle
        if idle and not self._waiting and self._closing is None:
            # Rule 1, the common case, without the general routing: the most recently idle
            # connection, taken off the stack. A dead one is dropped, and routing starts over.
            entry = idle.popitem()[0]
            if entry.conn.closed:
                self._discard(entry)
                entry = self._route()
        else:
            entry = self._route()"""


def variant(cond: str) -> str:
    return f"""        idle = self._idle
        if idle and not self._waiting and self._closing is None:
            entry = None
            if {cond}:
                router = self._router
                slot = router.best_below(
                    self.max_inflight_per_conn, 2 * router.expected(query)
                )
                if slot is not None:
                    entry = self._slots[slot]
                    if entry.conn.closed:
                        entry = None
            if entry is None:
                entry = idle.popitem()[0]
                if entry.conn.closed:
                    self._discard(entry)
                    entry = self._route()
        else:
            entry = self._route()"""


(V / "h2.py").write_text(rep(cur, OLD, variant("True")))
(V / "h2r.py").write_text(rep(cur, OLD, variant("len(idle) <= self.max_size // 4")))
