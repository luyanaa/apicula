"""Program pip fuse bits into an HqFpga `.bit` image.

The fuse table is the output of :mod:`xist.fusemap` (or the sample in
``testdata/SL2-12E_pip_fuses_sample.json``).  A pip that is present in a route
sets its fuse bit; an absent pip clears it (with a possible polarity inversion).

This is a first, partial writer: only pips that have a validated fuse entry are
programmed.  LUT/DFF/PIO configuration and the bitstream checksum are separate
steps.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from .bitstream import Bitstream
from .xpn import parse_xpn


def load_fusemap(path: str | Path) -> list[dict]:
    data = json.loads(Path(path).read_text())
    return data["matches"] if isinstance(data, dict) and "matches" in data else data


def _set(bs: Bitstream, r: int, c: int, val: bool) -> None:
    bs.rows[r] = bs.rows[r][:c] + ("1" if val else "0") + bs.rows[r][c + 1:]


def program(bs: Bitstream, pips: set[tuple[str, str]], fusemap: list[dict]) -> int:
    """Set every fuse in *fusemap* according to pip presence; return count."""
    n = 0
    for entry in fusemap:
        pip = tuple(entry["pip"])
        r, c = entry["pos"]
        present = pip in pips
        val = (not present) if entry.get("invert") else present
        _set(bs, r, c, val)
        n += 1
    return n


def program_relative(bs: Bitstream, pips: set[tuple[str, str]],
                     relmap: list[dict], columns: dict) -> int:
    """Program pips using tile-relative fuse entries (any tile)."""
    index = {(e["a"], e["b"]): e for e in relmap}
    n = 0
    for a, b in pips:
        ma = re.match(r"^R(\d+)C(\d+)_(.+)$", a)
        mb = re.match(r"^R(\d+)C(\d+)_(.+)$", b)
        m = ma or mb
        if not m:
            continue
        r, col = int(m.group(1)), int(m.group(2))
        la = ma.group(3) if ma else a
        lb = mb.group(3) if mb else b
        e = index.get((la, lb))
        if not e:
            continue
        origin = columns.get(str(col)) or columns.get(col)
        if origin is None:
            continue
        # pip is present by construction; inverted entries store 0
        val = (not e["invert"])
        br = 29 * r + e["rel"][0]
        bc = origin + e["rel"][1]
        if 0 <= br < bs.nrows and 0 <= bc < bs.ncols:
            _set(bs, br, bc, val)
            n += 1
    return n


def pips_from_xpn(path: str | Path) -> set[tuple[str, str]]:
    pips = set()
    for sig in parse_xpn(path).values():
        pips.update(sig.route)
    return pips


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="program pip fuses into a .bit")
    ap.add_argument("--base", required=True)
    ap.add_argument("--xpn", required=True, help="routed XPN used as the pip source")
    ap.add_argument("--map", required=True, help="fusemap JSON (xist.fusemap output)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    bs = Bitstream.read(args.base)
    pips = pips_from_xpn(args.xpn)
    n = program(bs, pips, load_fusemap(args.map))
    bs.write(args.out)
    print(f"programmed {n} fuses -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
