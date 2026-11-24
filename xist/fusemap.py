"""Correlate routed pips (XPN) with bitstream bits.

The vendor tool is deterministic and XPN names every wire used by a net.  By
running the same netlist with many different pin assignments we get a set of
bitstreams whose *routing* varies while the logic stays the same.  For every
bit position we build a signature over the runs; for every pip we build the
presence signature.  A unique bit signature equal to a unique pip signature
(or its complement) is a strong candidate for that pip's fuse bit.

This is the first step of the pip -> fuse map; it is not yet a complete map.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

from .bitstream import Bitstream
from .xpn import parse_xpn


def bit_signatures(bits: list[Bitstream]) -> dict[int, list[tuple[int, int]]]:
    n = len(bits)
    rows = [b.rows for b in bits]
    nr, nc = bits[0].nrows, bits[0].ncols
    sigpos: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for r in range(nr):
        rr = [rows[i][r] for i in range(n)]
        for c in range(nc):
            s = 0
            for i in range(n):
                if rr[i][c] == "1":
                    s |= 1 << i
            if s not in (0, (1 << n) - 1):
                sigpos[s].append((r, c))
    return sigpos


def pip_signatures(xpns: list[Path]) -> dict[int, list[tuple[str, str]]]:
    n = len(xpns)
    sets = []
    allp = set()
    for p in xpns:
        ps = set()
        for sig in parse_xpn(p).values():
            ps.update(sig.route)
        sets.append(ps)
        allp |= ps
    sigs: dict[int, list[tuple[str, str]]] = defaultdict(list)
    for pip in allp:
        s = 0
        for i in range(n):
            if pip in sets[i]:
                s |= 1 << i
        if s not in (0, (1 << n) - 1):
            sigs[s].append(pip)
    return sigs


def correlate(bit_paths: list[str | Path], xpn_paths: list[str | Path],
              unique_only: bool = True) -> list[dict]:
    bits = [Bitstream.read(str(p)) for p in bit_paths]
    xpns = [Path(p) for p in xpn_paths]
    if len(bits) != len(xpns):
        raise ValueError("need one XPN per bitstream")
    n = len(bits)
    bsig = bit_signatures(bits)
    psig = pip_signatures(xpns)
    out = []
    for s, plist in psig.items():
        if unique_only and len(plist) != 1:
            continue
        for key, inv in ((s, False), (((1 << n) - 1) ^ s, True)):
            positions = bsig.get(key, [])
            if unique_only and len(positions) != 1:
                continue
            for pos in positions:
                out.append(dict(pip=list(plist[0]) if len(plist) == 1 else None,
                                pips=[list(p) for p in plist],
                                pos=list(pos), invert=inv))
    return out


def relative_map(matches: list[dict], columns: dict) -> list[dict]:
    """Convert absolute candidates into tile-relative pip fuse entries.

    ``columns`` maps tile column -> bitstream column origin (slice D block).
    The returned entry has local wire names and a (row, col) offset that can be
    applied at any tile with the same local pip.
    """
    import re
    groups: dict[tuple[str, str], list[tuple[int, int, bool]]] = defaultdict(list)

    def split(w):
        m = re.match(r"^R(\d+)C(\d+)_(.+)$", w)
        return (int(m.group(1)), int(m.group(2)), m.group(3)) if m else None

    for c in matches:
        a, b = c["pip"]
        t = split(a) or split(b)
        if not t:
            continue
        r, col, _ = t
        origin = columns.get(str(col)) or columns.get(col)
        if origin is None:
            continue
        rel = (c["pos"][0] - 29 * r, c["pos"][1] - origin)
        la = split(a)[2] if split(a) else a
        lb = split(b)[2] if split(b) else b
        groups[(la, lb)].append((rel[0], rel[1], bool(c.get("invert"))))

    out = []
    for (la, lb), vals in sorted(groups.items()):
        # mode of the relative offsets (a pip type may occur at a tile edge
        # with a different edge tile type; keep the most common)
        counts = defaultdict(int)
        for v in vals:
            counts[v] += 1
        (dr, dc, inv), _ = max(counts.items(), key=lambda kv: kv[1])
        out.append(dict(a=la, b=lb, rel=[dr, dc], invert=inv, samples=len(vals)))
    return out


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="pip <-> bitstream correlation")
    ap.add_argument("--bits", nargs="+", required=True)
    ap.add_argument("--xpns", nargs="+", required=True)
    ap.add_argument("--out", default="pipfuses.json")
    ap.add_argument("--all", action="store_true", help="allow non-unique matches")
    args = ap.parse_args(argv)
    matches = correlate(args.bits, args.xpns, unique_only=not args.all)
    Path(args.out).write_text(json.dumps(matches, indent=1) + "\n")
    print(f"{len(matches)} candidate pip fuses -> {args.out}")
    for m in matches[:20]:
        print(("~" if m["invert"] else " "), m["pips"], "->", m["pos"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
