"""Parser for the vendor BFD (Bitstream Format Database) dump.

HqFpga can dump its complete bitstream database with::

    dv.setup SEALION SL2-12E-8F256C
    'bfd.dumpbfd /tmp/SL2-12E.bfd
    exit

The dump contains, per tile type, a ``Rows x Columns`` RAM grid listing every
fuse bit, plus Boolean equations defining routing fuses in terms of
``arcval(source_wire, destination)`` arcs.  This is the authoritative routing
fuse map; the fuzz campaigns only ever sampled it.

Each tile section looks like::

    Tile "PLC" Rows=29 Columns=48 Rams=1392 Nodes=306
    {
       Sites { SLICE_A Row=13, ... }
       Nodes { A0, A1, ... }
       Ram Row=0 { <48 fuse names> }
       ...
       XROUTING/XC67D45/MC1_SB_0 = (((arcval(H00R0000,C6) || ...));
    }

The Ram grid address is tile-local: ``(ram_row, ram_col)``.  See
:mod:`xist.bfdmap` for the calibration to absolute bitstream coordinates.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

_TILE_RE = re.compile(r'^Tile "([^"]+)"\s+Rows=(\d+)\s+Columns=(\d+)')
_RAM_RE = re.compile(r"^\s*Ram Row=(\d+)\s*$")
_EQ_RE = re.compile(r"^\s*([A-Za-z0-9_/\[\].]+)\s*=\s*(.*)$")
_ARC_RE = re.compile(r"arcval\(([^,]+),([^)]+)\)")
# Simple negated-guard form used by the PIO input-delay fuses:
#   (!arcval(JPADDIA_PIO,JDIA) && wire(JDIA))
_SPECIAL_RE = re.compile(
    r"^\s*\(*\s*!arcval\(\s*([^,]+?)\s*,\s*([^)]+?)\s*\)\s*"
    r"&&\s*wire\(\s*([^)]+?)\s*\)\s*\)*\s*$")
_CONFIG_RE = re.compile(r"(?:config|property|comp)\(([^)]*)\)")


@dataclass
class BfdTile:
    name: str
    rows: int
    cols: int
    ram: dict[int, list[str]] = field(default_factory=dict)
    equations: dict[str, str] = field(default_factory=dict)
    nodes: list[str] = field(default_factory=list)
    aliases: dict[str, str] = field(default_factory=dict)
    sites: dict[str, int] = field(default_factory=dict)

    def fuse_positions(self, fuse: str) -> list[tuple[int, int]]:
        return [(r, c) for r, row in sorted(self.ram.items())
                for c, n in enumerate(row) if n == fuse]

    def arcs(self, include_conditional: bool = False
             ) -> dict[tuple[str, str], list[dict]]:
        """Routing arc ``(source, destination)`` -> implementing fuses.

        A fuse may implement several arcs (a mux), and one arc may be
        implemented by several fuses, so the value is a list.

        By default fuses whose equation also contains a ``config()`` or
        ``property()`` term are skipped: their value depends on slice
        configuration, not just on the route, so an arc-only writer must not
        drive them.  Pass ``include_conditional=True`` for the logical arc set.
        """
        out: dict[tuple[str, str], list[dict]] = {}
        for fuse, expr in self.equations.items():
            if not include_conditional and ("config(" in expr
                                            or "property(" in expr):
                continue
            positions = self.fuse_positions(fuse)
            if not positions:
                continue
            seen = set()
            for m in _ARC_RE.finditer(expr):
                arc = (m.group(1).strip(), m.group(2).strip())
                if arc in seen:
                    continue
                seen.add(arc)
                for (r, c) in positions:
                    out.setdefault(arc, []).append(
                        dict(tile=self.name, row=r, col=c, fuse=fuse))
        return out

    def special_fuses(self) -> dict[str, dict]:
        """Fuses of the form ``!arcval(a,b) && wire(w)`` (inverted + guarded)."""
        out: dict[str, dict] = {}
        for fuse, expr in self.equations.items():
            m = _SPECIAL_RE.match(expr)
            if m:
                out[fuse] = dict(arc=[m.group(1), m.group(2)],
                                 guard=m.group(3), invert=True)
        return out

    def arc_set(self) -> set[tuple[str, str]]:
        """All arcs, independent of whether a Ram fuse was found."""
        out = set()
        for expr in self.equations.values():
            for m in _ARC_RE.finditer(expr):
                out.add((m.group(1).strip(), m.group(2).strip()))
        return out


def parse(path: str | Path) -> dict[str, BfdTile]:
    lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    starts = [(i, m) for i, l in enumerate(lines)
              for m in [_TILE_RE.match(l)] if m]
    tiles: dict[str, BfdTile] = {}
    for k, (idx, m) in enumerate(starts):
        end = starts[k + 1][0] if k + 1 < len(starts) else len(lines)
        sec = lines[idx:end]
        tile = BfdTile(m.group(1), int(m.group(2)), int(m.group(3)))

        def block(start: int) -> tuple[list[str], int]:
            """Read a flat comma list in braces starting at *start*.

            Returns the items and the index just past the closing brace.
            """
            i = start
            if i < len(sec) and sec[i].strip() == "{":
                i += 1
            items: list[str] = []
            while i < len(sec) and sec[i].strip() != "}":
                s = sec[i].strip()
                if s:
                    items += [x.strip() for x in s.split(",") if x.strip()]
                i += 1
            return items, i + 1

        i = 0
        while i < len(sec):
            s = sec[i].strip()
            if s in ("", "{", "}"):
                i += 1
                continue
            if s in ("Sites", "Nodes"):
                items, i = block(i + 1)
                if s == "Nodes":
                    for it in items:
                        if "=" in it:
                            canon, alias = (x.strip() for x in it.split("=", 1))
                            tile.aliases[alias] = canon
                            if canon not in tile.nodes:
                                tile.nodes.append(canon)
                        elif it not in tile.nodes:
                            tile.nodes.append(it)
                else:
                    for it in items:
                        mm = re.match(r"(\S+)\s+Row=(\d+)", it)
                        if mm:
                            tile.sites[mm.group(1)] = int(mm.group(2))
                continue
            rm = _RAM_RE.match(sec[i])
            if rm:
                row = int(rm.group(1))
                items, i = block(i + 1)
                tile.ram[row] = items
                continue
            em = _EQ_RE.match(sec[i])
            if em:
                name, expr = em.group(1), em.group(2)
                j = i + 1
                while not expr.rstrip().endswith(";") and j < len(sec):
                    expr += " " + sec[j].strip()
                    j += 1
                tile.equations[name] = expr.replace(";", "").strip()
                i = j
                continue
            i += 1
        tiles[tile.name] = tile
    return tiles


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="parse a HqFpga BFD dump")
    ap.add_argument("bfd")
    ap.add_argument("--tile", default="PLC")
    ap.add_argument("--out", default=None)
    ap.add_argument("--all", action="store_true",
                    help="export every tile type instead of one")
    args = ap.parse_args(argv)
    tiles = parse(args.bfd)
    print(f"{len(tiles)} tile types")
    if args.all:
        data = {}
        for name, t in tiles.items():
            arcs = t.arcs()
            data[name] = {
                "rows": t.rows, "cols": t.cols,
                "ram_rows": len(t.ram), "nodes": len(t.nodes),
                "equations": len(t.equations), "arcs": len(t.arc_set()),
                "arc_fuses": {f"{a}->{b}": v for (a, b), v in sorted(arcs.items())},
            }
        if args.out:
            Path(args.out).write_text(json.dumps(data, indent=1) + "\n")
            print(f"wrote {args.out} ({sum(len(v['arc_fuses']) for v in data.values())} arc entries)")
        return 0
    t = tiles.get(args.tile)
    if not t:
        print(f"no tile {args.tile!r}; available: {list(tiles)[:12]}")
        return 1
    arcs = t.arcs()
    print(f"{t.name}: {t.rows}x{t.cols}, ram_rows={len(t.ram)} "
          f"(row length {len(next(iter(t.ram.values()))) if t.ram else 0}), "
          f"nodes={len(t.nodes)}, sites={t.sites}")
    print(f"  equations={len(t.equations)} arcs={len(t.arc_set())} "
          f"arc-fuses={len(arcs)}")
    if args.out:
        data = {f"{a}->{b}": v for (a, b), v in sorted(arcs.items())}
        Path(args.out).write_text(json.dumps(data, indent=1) + "\n")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
