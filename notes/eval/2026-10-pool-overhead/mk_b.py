from pathlib import Path

V = Path(__file__).parent / "v"
SRC = Path("<repo>/src/fastpg/_pool.py")


def rep(s: str, a: str, b: str) -> str:
    assert s.count(a) == 1, a
    return s.replace(a, b)


cur = SRC.read_text()
(V / "a.py").write_text(cur)

W1 = [
    (
        """        entry.expected_ns += cost
        self._idle.pop(entry, None)  # only SHARED entries take shared statements
        return token""",
        """        entry.expected_ns += cost
        return token""",
    ),
    (
        """        idle = self._idle
        while idle:
            entry = next(reversed(idle))
            if not entry.conn.closed:
                return entry
            self._discard(entry)  # died since its last statement""",
        """        idle = self._idle
        while idle:
            entry = idle.popitem()[0]  # off the stack: the caller begins a statement on it
            if not entry.conn.closed:
                return entry
            self._discard(entry)  # died since its last statement""",
    ),
    (
        """        waiting = self._waiting
        while waiting:
            entry = self._best_slot()
            if entry is None:
                break
            future, sql = waiting.popleft()
            if not future.done():
                future.set_result((entry, self._begin(entry, sql)))""",
        """        waiting = self._waiting
        while waiting:
            future, sql = waiting[0]
            if future.done():
                waiting.popleft()
                continue
            entry = self._best_slot()
            if entry is None:
                break
            waiting.popleft()
            future.set_result((entry, self._begin(entry, sql)))""",
    ),
]
INLINE = [
    (
        """        entry = self._route()
        if entry is None:
            entry, token = await self._wait(query)""",
        """        idle = self._idle
        if idle and not self._waiting and self._closing is None:
            entry = idle.popitem()[0]
            if entry.conn.closed:
                self._discard(entry)
                entry = self._route()
        else:
            entry = self._route()
        if entry is None:
            entry, token = await self._wait(query)""",
    ),
]
s = cur
for a, b in W1:
    s = rep(s, a, b)
(V / "b1.py").write_text(s)
for a, b in INLINE:
    s = rep(s, a, b)
(V / "b2.py").write_text(s)
