#!yeet
"""Summaries of a macOS ``sample`` call tree (main thread).

    .venv/bin/yeet ./notes/eval/2026-10-hot-path/hot.py -n 250000 --rounds 1 &
    sample <python pid> 60 1 -file /tmp/sample.txt
    uv run yeet ./notes/eval/2026-10-hot-path/sagg.py incl /tmp/sample.txt __pymethod_fetch__ ...
    uv run yeet ./notes/eval/2026-10-hot-path/sagg.py tree /tmp/sample.txt __pymethod_on_readable__

``incl``: inclusive sample counts of the frames matching each pattern (outermost match).
``tree``: the subtree under every frame matching a pattern, merged by frame path.
One sample is 1 ms; divide by the number of queries in the sampled window for µs per query.
"""

import re
from pathlib import Path

LINE = re.compile(r"^(?P<indent>[ +!:|]*)(?P<count>\d+) (?P<frame>.*)$")


def _clean(frame: str) -> str:
    frame = re.sub(r"\s+\(in [^)]*\).*$", "", frame)
    frame = re.sub(r"::h[0-9a-f]{16}", "", frame)
    return frame.replace("_$u7b$$u7b$closure$u7d$$u7d$", "{closure}")


def _nodes(path: str, thread: str) -> list[tuple[int, str, int]]:
    """``(depth, frame, count)`` per call-tree line of ``thread``."""
    lines = Path(path).read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if thread in line)
    nodes: list[tuple[int, str, int]] = []
    base = None
    for line in lines[start + 1 :]:
        m = LINE.match(line)
        if not m:
            break
        depth = len(m["indent"])
        if base is None:
            base = depth
        if depth <= base - 2:
            break
        nodes.append((depth, _clean(m["frame"]), int(m["count"])))
    return nodes


def tree(path: str, pattern: str, *, minimum: int = 5, thread: str = "main-thread") -> None:
    merged: dict[tuple[str, ...], int] = {}
    root_depth: int | None = None
    stack: list[tuple[int, str]] = []
    for depth, frame, count in _nodes(path, thread):
        if root_depth is not None and depth <= root_depth:
            root_depth = None
        if root_depth is None:
            if pattern in frame:
                root_depth = depth
                stack = [(depth, frame)]
                merged[(frame,)] = merged.get((frame,), 0) + count
            continue
        while stack and stack[-1][0] >= depth:
            stack.pop()
        stack.append((depth, frame))
        key = tuple(f for _, f in stack)
        merged[key] = merged.get(key, 0) + count
    for key in sorted(merged):
        if merged[key] >= minimum:
            print(f"{'  ' * (len(key) - 1)}{merged[key]} {key[-1][:140]}")


def incl(path: str, patterns: list[str], *, thread: str = "main-thread") -> None:
    nodes = _nodes(path, thread)
    for pattern in patterns:
        total = 0
        open_depth: int | None = None
        for depth, frame, count in nodes:
            if open_depth is not None and depth <= open_depth:
                open_depth = None
            if open_depth is None and pattern in frame:
                total += count
                open_depth = depth
        print(f"{total:7d}  {pattern}")
