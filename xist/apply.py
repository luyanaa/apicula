"""Apply PIO configuration from an XPN to a `.bit` template.

This is the Python implementation of the PIO part of the bitstream writer.  The
C++ uarch writer is still blocked on the (unknown) checksum, so the fused data
is applied here instead; the same sidecar/metadata is used.

    python -m xist.apply --base template.bit --xpn design.xpn --out out.bit
    python -m xist.apply --base template.bit --xpn design.xpn --check vendor.bit

Only edge PIO configuration is handled here; routing fuses use
:func:`xist.program.program_routing`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .bfdmap import load_calibration
from .bitstream import Bitstream
from .bfd_expr import parse_cached
from .pioconfig import bits_from_equations, load_profile
from .program import _set
from .xpn import parse_comps

HERE = Path(__file__).resolve().parent
TESTDATA = HERE / "testdata"
PIO_FILE = TESTDATA / "SL2-12E_bfd_pio.json"

PROFILE = {"L": "PIC_L5", "R": "PIC_R5", "T": "PIC_T5", "B": "PIC_B5"}


def _config(xpn_path, pio_json: str | Path = PIO_FILE):
    data = json.loads(Path(pio_json).read_text())
    return data, {s: load_profile(pio_json, PROFILE[s])[0] for s in "LRTB"}


def apply_pio(bs: Bitstream, xpn_path: str | Path,
              pio_json: str | Path = PIO_FILE, calibration=None) -> int:
    """Program all edge PIO configuration bits for the XPN's pads."""
    return apply_pio_comps(bs, list(parse_comps(xpn_path).values()),
                           pio_json, calibration)


def profile_config_equations(profile: dict) -> list:
    """Pure config fuses of an edge tile profile (gives IOLOGIC/PU/PWRGRD)."""
    out = []
    for name, expr in (profile.get("fuse_exprs") or {}).items():
        if "arcval" in expr:
            continue
        if "config(" not in expr and "property(" not in expr:
            continue
        out.append((name, parse_cached(expr),
                    [tuple(p) for p in profile.get("fuses", {}).get(name, [])]))
    return out


def apply_pio_comps(bs: Bitstream, comps, pio_json: str | Path = PIO_FILE,
                    calibration=None, profile_eqs: dict | None = None) -> int:
    """Program PIO config from already-parsed/synthesised Comp objects.

    ``profile_eqs`` optionally maps a side (L/R/T/B) to the extra pure-config
    fuses of that edge tile profile, so IOLOGIC/PULLUP fuses are cleared too.
    """
    if not comps:
        return 0
    data, eqs = _config("", pio_json)
    cal = calibration or load_calibration()
    groups: dict[tuple[str, int], list] = {}
    for comp in comps:
        site = comp.site
        if not site or len(site) < 4 or site[0] != "P":
            continue
        side = site[1]
        if side not in "LRTB" or not site[2:-1].isdigit():
            continue
        groups.setdefault((side, int(site[2:-1])), []).append(comp)

    n = 0
    for (side, pos), comps in groups.items():
        if side in "LR":
            base, rbase = 2375 if side == "L" else 60, None
            tile_row = pos
        else:
            if pos not in cal.column_base:
                continue
            delta = data["column_delta"].get(side, {}).get(str(pos), 0)
            base = cal.column_base[pos] + delta
            rbase = data["row_base"][side]
            tile_row = pos
        side_eqs = list(eqs[side])
        if profile_eqs and side in profile_eqs:
            side_eqs.extend(profile_eqs[side])
        for (br, bc, val) in bits_from_equations(side_eqs, comps, tile_row,
                                                 base, rbase):
            if 0 <= br < bs.nrows and 0 <= bc < bs.ncols:
                _set(bs, br, bc, val)
                n += 1
    return n


def _positions(xpn_path: str | Path, pio_json: str | Path = PIO_FILE,
               calibration=None):
    data, eqs = _config(xpn_path, pio_json)
    cal = calibration or load_calibration()
    comps = list(parse_comps(xpn_path).values())
    groups: dict[tuple[str, int], list] = {}
    for comp in comps:
        site = comp.site
        if not site or len(site) < 4 or site[0] != "P":
            continue
        side = site[1]
        if side not in "LRTB" or not site[2:-1].isdigit():
            continue
        groups.setdefault((side, int(site[2:-1])), []).append(comp)
    out = []
    for (side, pos), cs in groups.items():
        if side in "LR":
            base, rbase, tile_row = (2375 if side == "L" else 60), None, pos
        else:
            if pos not in cal.column_base:
                continue
            delta = data["column_delta"].get(side, {}).get(str(pos), 0)
            base, rbase, tile_row = cal.column_base[pos] + delta, data["row_base"][side], pos
        out.extend(bits_from_equations(eqs[side], cs, tile_row, base, rbase))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="apply XPN PIO config to a .bit")
    ap.add_argument("--base", required=True)
    ap.add_argument("--xpn", required=True)
    ap.add_argument("--out", default=None)
    ap.add_argument("--check", default=None,
                    help="compare with this vendor .bit")
    ap.add_argument("--pio", default=str(PIO_FILE))
    args = ap.parse_args(argv)
    bs = Bitstream.read(args.base)
    n = apply_pio(bs, args.xpn, args.pio)
    bs.update_checksum()
    if args.out:
        bs.write(args.out)
    print(f"programmed {n} PIO configuration bits")
    if args.check:
        target = Bitstream.read(args.check)
        positions = _positions(args.xpn, args.pio)
        ok = sum(bs.rows[br][bc] == target.rows[br][bc]
                 for br, bc, _v in positions)
        print(f"check: {ok}/{len(positions)} bits match {args.check}")
        return 0 if ok == len(positions) else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
