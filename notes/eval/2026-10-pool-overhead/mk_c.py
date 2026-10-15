from pathlib import Path

V = Path(__file__).parent / "v"
SRC = Path("<repo>/src/fastpg/_pool.py")


def rep(s: str, a: str, b: str) -> str:
    assert s.count(a) == 1, a
    return s.replace(a, b)


cur = SRC.read_text()
(V / "b.py").write_text(cur)

OLD_TAIL = """        entry.done_ns = now
        if not started:
            entry.last_ns = now
        if (error is not None and (_is_fatal(error) or entry.conn.closed)) or (
            entry.state == _Entry.RETIRING and not entry.started
        ):
            self._discard(entry)
        elif not started:
            self._place(entry)  # idle now: on top of its stack (unless GONE)
        if self._waiting or self._acquiring:
            self._dispatch()"""
NEW_TAIL = """        entry.done_ns = now
        if error is None and not entry.state:  # succeeded on a SHARED connection
            if not started:
                entry.last_ns = now
                self._idle[entry] = None  # idle now: on top of the stack
        else:
            if not started:
                entry.last_ns = now
            if (error is not None and (_is_fatal(error) or entry.conn.closed)) or (
                entry.state == _Entry.RETIRING and not started
            ):
                self._discard(entry)
            elif not started:
                self._place(entry)  # idle now: on top of its stack (unless GONE)
        if self._waiting or self._acquiring:
            self._dispatch()"""
c1 = rep(cur, OLD_TAIL, NEW_TAIL)
(V / "c1.py").write_text(c1)

OLD_LEARN_CALL = """        if sql is not None:
            self._learn(entry, sql, sent_ns, now)"""
c2 = rep(c1, OLD_LEARN_CALL, """        if sql is not None:
            self._learn(sql, now - max(sent_ns, entry.done_ns))""")
c2 = rep(c2, "    def _learn(self, entry: _Entry, sql: str, sent_ns: int, now: int) -> None:", "    def _learn(self, sql: str, service: int) -> None:")
c2 = rep(c2, "        service = now - max(sent_ns, entry.done_ns)\n", "")
(V / "c2.py").write_text(c2)

c3 = rep(c1, OLD_LEARN_CALL, """        if sql is not None:
            done = entry.done_ns
            service = now - (sent_ns if sent_ns > done else done)
            costs = self._costs
            old = costs.get(sql)
            if old is None:
                if len(costs) >= self._MAX_COSTS:
                    costs.clear()
                costs[sql] = service
            else:
                costs[sql] = old + (service - old) // 4""")
(V / "c3.py").write_text(c3)
