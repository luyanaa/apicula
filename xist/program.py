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
