"""A tiny consistency checker for the text .bba emitted for nextpnr.

``bbasm`` will do the real work, but it is useful to catch dangling labels
before shipping a 150 KB file to a nextpnr build.
"""

from __future__ import annotations

import re
from pathlib import Path

_REF = re.compile(r"^(?:ref|slice)\s+(\S+)")


def check(path: str | Path) -> list[str]:
    labels: set[str] = set()
    refs: list[tuple[int, str]] = []
    depth = 0
    stack_ok = True
    for lineno, line in enumerate(Path(path).read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        op, _, rest = line.partition(" ")
        if op == "label":
            labels.add(rest.strip())
        elif op in ("ref", "slice"):
            refs.append((lineno, rest.split()[0]))
        elif op == "push":
            depth += 1
        elif op == "pop":
            depth -= 1
            if depth < 0:
                stack_ok = False
    problems = []
    for lineno, name in refs:
        if name not in labels:
            problems.append(f"line {lineno}: reference to undefined label {name!r}")
    if depth != 0:
        problems.append(f"push/pop imbalance: depth={depth}")
    if not stack_ok:
        problems.append("pop without push")
    return problems
