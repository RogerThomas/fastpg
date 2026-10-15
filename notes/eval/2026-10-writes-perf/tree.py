#!yeet
"""Condense a macOS `sample` call graph: main thread, nodes >= min samples, short names."""

import re
from pathlib import Path

LINE = re.compile(r"^(?P<indent>[ +!:|]*)(?P<count>\d+) (?P<name>.*)$")


def short(name: str) -> str:
    name = re.sub(r"\s+\(in [^)]*\).*$", "", name)
    name = re.sub(r"::h[0-9a-f]{16}", "", name)
    name = name.replace("$LT$", "<").replace("$GT$", ">").replace("$u20$", " ")
    name = name.replace("$u7b$", "{").replace("$u7d$", "}").replace("..", "::")
    name = name.replace("$C$", ",").replace("$RF$", "&")
    return name[:150]


def main(path: str, min_samples: int = 20, max_depth: int = 40, thread: int = 0) -> None:
    lines = Path(path).read_text().splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("Call graph:"))
    seen_threads = -1
    for line in lines[start + 1 :]:
        if not line.strip():
            break
        m = LINE.match(line.strip("\n")[4:]) if line.startswith("    ") else None
        if m is None:
            continue
        depth = len(m.group("indent")) // 2
        if depth == 0:
            seen_threads += 1
        if seen_threads != thread:
            continue
        count = int(m.group("count"))
        if count >= min_samples and depth <= max_depth:
            print(f"{'  ' * depth}{count} {short(m.group('name'))}")
