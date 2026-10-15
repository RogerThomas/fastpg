from pathlib import Path

V = Path(__file__).parent / "v"
s = (V / "base.py").read_text()
start = s.index("    async def _query(")
end = s.index("    # --- exclusive checkouts")
head = '''    async def _query(
        self,
        kind: int,
        query: str | Template,
        params: tuple[object, ...],
        into: type[Any] | None,
        strategy: Strategy,
    ) -> Any:  # noqa: ANN401 - typed by the public overloads
'''
PICK = '''        if isinstance(query, Template):
            query, params = self._templates.bind(query, params)
        idle = self._idle
        if idle and self._closing is None and not self._waiting:
            entry = idle.popitem()[0]
            if entry.conn.closed:
                raise RuntimeError
'''
BEGIN = '''        self._token = token = self._token + 1
        cost = self._costs.get(query, 50_000)
        entry.started[token] = (time.perf_counter_ns(), cost)
        entry.expected_ns += cost
'''
CALL = '''        try:
            result = await entry.conn.select_one_or_none(query, *params)
        except BaseException:
            raise
'''
END0 = '''        started = entry.started
        sent_ns, cost = started.pop(token)
        entry.expected_ns -= cost
        now = time.perf_counter_ns()
'''
LEARN = '''        done = entry.done_ns
        service = now - (sent_ns if sent_ns > done else done)
        costs = self._costs
        old = costs.get(query)
        if old is None:
            costs[query] = service
        else:
            costs[query] = old + (service - old) // 4
'''
END1 = '''        entry.done_ns = now
        if not started:
            entry.last_ns = now
            idle[entry] = None
        if self._waiting or self._acquiring:
            self._dispatch()
        return result
'''
bodies = {
    "sk0": '''        idle = self._idle
        entry = idle.popitem()[0]
        result = await entry.conn.select_one_or_none(query, *params)
        idle[entry] = None
        return result
''',
    "sk1": PICK + CALL + '''        idle[entry] = None
        if self._waiting or self._acquiring:
            self._dispatch()
        return result
''',
    "sk2": PICK + BEGIN + CALL + END0 + END1,
    "sk3": PICK + BEGIN + CALL + END0 + LEARN + END1,
}
for k, b in bodies.items():
    (V / f"{k}.py").write_text(s[:start] + head + b + "\n" + s[end:])
