from pathlib import Path

V = Path(__file__).parent / "v"
SRC = Path("<repo>/src/fastpg/_pool.py")


def rep(s: str, a: str, b: str) -> str:
    assert s.count(a) == 1, a
    return s.replace(a, b)


cur = SRC.read_text()
(V / "c.py").write_text(cur)

s = cur
s = rep(
    s,
    """    expected_ns: int = 0  # sum of the expected service times in `started`
""",
    """    expected_ns: int = 0  # sum of the expected service times in `started`
    oldest_ns: int = 0  # send time of the oldest statement in `started` (if any)
""",
)
s = rep(
    s,
    """        cost = self._costs.get(sql, self._DEFAULT_COST_NS)
        entry.started[token] = (time.perf_counter_ns(), cost)
        entry.expected_ns += cost
        return token""",
    """        cost = self._costs.get(sql, self._DEFAULT_COST_NS)
        sent = time.perf_counter_ns()
        started = entry.started
        if not started:
            entry.oldest_ns = sent
        started[token] = (sent, cost)
        entry.expected_ns += cost
        return token""",
)
s = rep(
    s,
    """        best: _Entry | None = None
        best_score = 0
        now = 0
        cap = self.max_inflight_per_conn
        for entry in self._entries:
            if entry.state:  # not _Entry.SHARED
                continue
            started = entry.started
            n = len(started)
            if not n:  # unreachable while _idle is in step; idle is still the best pick
                self._idle.pop(entry, None)
                best = entry
                break
            if n < cap:
                if not now:
                    now = time.perf_counter_ns()
                # The queue's expected work, or how long it has been busy if that's longer
                # (a statement overrunning its estimate is assumed to keep running). Not
                # `expected - age`: that rates a connection "almost done" with a 20 ms
                # statement as free, and measured p99 got worse when estimates overran.
                age = now - next(iter(started.values()))[0]
                score = max(entry.expected_ns, age)
                if best is None or score < best_score:
                    best, best_score = entry, score""",
    """        best: _Entry | None = None
        best_score = 0
        now = time.perf_counter_ns()
        cap = self.max_inflight_per_conn
        for entry in self._entries:
            if entry.state:  # not _Entry.SHARED
                continue
            n = len(entry.started)
            if not n:  # unreachable while _idle is in step; idle is still the best pick
                self._idle.pop(entry, None)
                best = entry
                break
            if n < cap:
                # The queue's expected work, or how long it has been busy if that's longer
                # (a statement overrunning its estimate is assumed to keep running). Not
                # `expected - age`: that rates a connection "almost done" with a 20 ms
                # statement as free, and measured p99 got worse when estimates overran.
                age = now - entry.oldest_ns
                expected = entry.expected_ns
                score = expected if expected > age else age
                if best is None or score < best_score:
                    best, best_score = entry, score""",
)
s = rep(
    s,
    """        sent_ns, cost = started.pop(token)
        entry.expected_ns -= cost""",
    """        sent_ns, cost = started.pop(token)
        if started and sent_ns == entry.oldest_ns:  # it was the oldest: the next one is
            entry.oldest_ns = next(iter(started.values()))[0]
        entry.expected_ns -= cost""",
)
(V / "d.py").write_text(s)
