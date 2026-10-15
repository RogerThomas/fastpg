#!yeet
"""Inclusive sample counts per function from a macOS `sample` call graph."""

import re
from collections import Counter
from pathlib import Path

LINE = re.compile(r"^(?P<lead>[\s+|:!]*)(?P<count>\d+) (?P<name>.+?)(?:  \(in (?P<lib>[^)]+)\).*)?$")


def _short(name: str) -> str:
    name = re.sub(r"::h[0-9a-f]{16}$", "", name)
    name = re.sub(r"\.llvm\.\d+", "", name)
    return name.replace("_$u7b$$u7b$closure$u7d$$u7d$", "{closure}")


def _callers(lines: list[str], rx: re.Pattern[str], levels: int) -> None:
    stack: list[tuple[int, str]] = []
    by_caller: Counter[str] = Counter()
    for line in lines:
        m = LINE.match(line)
        if not m:
            continue
        depth = len(m["lead"])
        name = _short(m["name"].strip())
        while stack and stack[-1][0] >= depth:
            stack.pop()
        if rx.search(name) and not any(rx.search(n) for _, n in stack):
            by_caller[" <- ".join(n for _, n in stack[-levels:][::-1])] += int(m["count"])
        stack.append((depth, name))
    for chain, count in by_caller.most_common(25):
        print(f"{count:6d}  {chain}")


def main(path: Path, pattern: str = "", top: int = 60) -> None:
    lines = path.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("Call graph:"))
    end = next(i for i, line in enumerate(lines) if line.startswith("Total number in stack"))
    stack: list[tuple[int, str]] = []
    inclusive: Counter[str] = Counter()
    for line in lines[start + 1 : end]:
        m = LINE.match(line)
        if not m:
            continue
        depth = len(m["lead"])
        name = _short(m["name"].strip())
        count = int(m["count"])
        while stack and stack[-1][0] >= depth:
            stack.pop()
        if all(n != name for _, n in stack):
            inclusive[name] += count
        stack.append((depth, name))
    rx = re.compile(pattern) if pattern else None
    if pattern.startswith("callers:"):
        _callers(lines[start + 1 : end], re.compile(pattern.removeprefix("callers:")), top)
        return
    for name, count in inclusive.most_common():
        if rx is None or rx.search(name):
            print(f"{count:6d}  {name}")
            top -= 1
            if top == 0:
                break
