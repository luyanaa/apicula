"""Parser for XiST XPN physical netlists (``xpn.write``).

XPN is a text database emitted after routing.  Each ``signal`` block lists the
connected cell pins and a ``route`` list of ``destination.source`` wire pairs,
which is exactly the information needed to learn the routing graph.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_SIGNAL_RE = re.compile(r'signal\s+(?:"([^"]+)"|([^\s{]+))\s*\{', re.S)
_PIN_RE = re.compile(r'\(\s*"?([^",]+)"?\s*,\s*([^)]+)\)')
_ROUTE_LINE_RE = re.compile(r'^\s*([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)\s*[,;]?\s*$')
_TILE_RE = re.compile(r"^(R\d+C\d+)_(.+)$")
_COMP_RE = re.compile(r'comp\s+(?:"([^"]+)"|([A-Za-z_][\w.$]*))\s*\{')
_SITE_RE = re.compile(r"\bsite\s+([A-Za-z0-9_]+);")
_PROGRAM_RE = re.compile(r"\bprogram(.*?);", re.S)
_QUOTED_RE = re.compile(r'"([^"]*)"')


@dataclass
class Signal:
    name: str
    pins: list[tuple[str, str]] = field(default_factory=list)
    route: list[tuple[str, str]] = field(default_factory=list)  # (src, dst)


def wire_tile(wire: str) -> tuple[int, int] | None:
    """Return (row, col) encoded in a wire name, or None."""
    m = _TILE_RE.match(wire)
    if not m:
        return None
    t = m.group(1)
    return int(t[1:t.index("C")]), int(t[t.index("C") + 1:])


def wire_local(wire: str) -> str:
    m = _TILE_RE.match(wire)
    return m.group(2) if m else wire


@dataclass
class Comp:
    name: str
    site: str | None
    program: str


def parse_comps(path: str | Path) -> dict[str, Comp]:
    """Parse the ``comp`` blocks of an XPN: name, placement site, program."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    comps: dict[str, Comp] = {}
    for m in _COMP_RE.finditer(text):
        name = m.group(1) or m.group(2)
        i, depth = m.end(), 1
        while i < len(text) and depth:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            i += 1
        body = text[m.end():i - 1]
        sm = _SITE_RE.search(body)
        prog = ""
        pm = _PROGRAM_RE.search(body)
        if pm:
            prog = " ".join(q.rstrip("\\ \n")
                            for q in _QUOTED_RE.findall(pm.group(1)))
        comps[name] = Comp(name, sm.group(1) if sm else None, prog)
    return comps


def parse_xpn(path: str | Path) -> dict[str, Signal]:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    signals: dict[str, Signal] = {}
    for m in _SIGNAL_RE.finditer(text):
        name = m.group(1) or m.group(2)
        start = m.end()
        depth = 1
        i = start
        while i < len(text) and depth:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            i += 1
        body = text[start:i - 1]
        sig = Signal(name)
        # split signal-pins and route sections
        pin_part, _, route_part = body.partition("route")
        for pm in _PIN_RE.finditer(pin_part):
            inst = pm.group(1).strip().strip('"')
            pin = pm.group(2).strip()
            if inst.lower() in ("drivers", "loads"):
                continue
            sig.pins.append((inst, pin))
        for line in route_part.splitlines():
            rm = _ROUTE_LINE_RE.match(line)
            if rm:
                dst, src = rm.group(1), rm.group(2)
                sig.route.append((src, dst))
        signals[name] = sig
    return signals


def routing_edges(paths: list[str | Path]):
    """Yield (src, dst) wire pairs across a corpus of XPN files."""
    seen = set()
    for p in paths:
        for sig in parse_xpn(p).values():
            for src, dst in sig.route:
                if (src, dst) not in seen:
                    seen.add((src, dst))
                    yield src, dst
