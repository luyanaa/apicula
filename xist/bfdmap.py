"""Calibration from BFD tile-local Ram addresses to absolute bitstream bits.

The vendor BFD dump (:mod:`xist.bfd`) gives each routing fuse as a tile-local
``(Ram row, Ram column)``.  For the SL2-12E bitstream the mapping is regular:

* rows:  ``bit_row = 29 * (tile_row - 1) + ram_row``
* cols:  ``bit_col = col_base[tile_col] - ram_col``

``col_base`` is the slice-D block origin of the tile column plus 44, with a
small per-column correction: ``columns.json`` origins are off by 0..2 for some
columns.  The correction (``column_delta``) was calibrated by evaluating the BFD
routing equations for real routed designs and choosing, per tile column, the
offset that reproduces the vendor bitstream.

Validation (305 routed ``io_pass`` bitstreams, tile types PLC):

    344040 fuse predictions, 344040 matches (100.000%), 7516 predicted set.

Only PLC has been validated end to end.  The same row/column rule should hold
for the other 48-column CIB tile types (they share the 29x48 Ram layout), but
that is inferred rather than measured.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .bfd import BfdTile, parse

HERE = Path(__file__).resolve().parent
TESTDATA = HERE / "testdata"
TILE_ROWS = 29

# Per tile-column correction to the columns.json origin (measured, see module
# docstring).  Missing columns use 0 (exact origin).
DEFAULT_COLUMN_DELTA = {
    2: -1, 4: -2, 13: -1, 24: -1, 38: -1, 40: -1, 42: -1, 48: -1,
}


@dataclass
class Calibration:
    """Tile-local Ram address -> absolute ``(bit_row, bit_col)``."""

    column_base: dict[int, int] = field(default_factory=dict)
    tile_rows: int = TILE_ROWS

    def row(self, tile_row: int, ram_row: int) -> int:
        return self.tile_rows * (tile_row - 1) + ram_row

    def col(self, tile_col: int, ram_col: int) -> int:
        return self.column_base[tile_col] - ram_col

    def bit(self, tile_row: int, tile_col: int, ram_row: int,
            ram_col: int) -> tuple[int, int]:
        return self.row(tile_row, ram_row), self.col(tile_col, ram_col)


def load_calibration(path: str | Path | None = None) -> Calibration:
    if path is None:
        path = TESTDATA / "SL2-12E_bitcol_base.json"
    data = json.loads(Path(path).read_text())
    return Calibration({int(k): v for k, v in data["column_base"].items()})


def calibration_from_columns(columns: dict, delta: dict[int, int] | None = None
                             ) -> Calibration:
    """Build a calibration from a ``columns.json`` mapping (origin + 44)."""
    delta = DEFAULT_COLUMN_DELTA if delta is None else delta
    out = {}
    for k, v in columns.items():
        if v is None:
            continue
        c = int(k)
        out[c] = v + 44 + delta.get(c, 0)
    return Calibration(out)


def arc_bits(tile: BfdTile, cal: Calibration, tile_row: int, tile_col: int
             ) -> dict[tuple[str, str], list[tuple[int, int]]]:
    """Routing arcs -> absolute fuse bit positions for one tile instance."""
    out = {}
    for arc, fuses in tile.arcs().items():
        pos = []
        for f in fuses:
            pos.append(cal.bit(tile_row, tile_col, f["row"], f["col"]))
        out[arc] = pos
    return out


def export(tile: BfdTile, cal: Calibration, out: Path) -> dict:
    """Write a compact, regeneration-friendly routing fuse table."""
    fuses: dict[str, list[list[int]]] = {}
    for name, positions in ((n, tile.fuse_positions(n))
                            for n in sorted(tile.equations)):
        if positions:
            fuses[name] = [list(p) for p in positions]
    arcs = {f"{a}->{b}": sorted({f["fuse"] for f in v})
            for (a, b), v in sorted(tile.arcs().items())}
    logical = {f"{a}->{b}" for a, b in tile.arc_set()}
    data = {
        "device": "SL2-12E-8F256C",
        "source": "vendor BFD dump ('bfd.dumpbfd), parsed by xist.bfd",
        "tile_type": tile.name,
        "rows": tile.rows,
        "cols": tile.cols,
        "ram_rows": len(tile.ram),
        "sites": tile.sites,
        "nodes": len(tile.nodes),
        "equations": len(tile.equations),
        "arcs": len(arcs),
        "logical_arcs": len(logical),
        "conditional_arcs": sorted(logical - set(arcs)),
        "routing_equations": sum(1 for e in tile.equations.values()
                                 if "arcval" in e and "config(" not in e
                                 and "property(" not in e),
        "fuses": fuses,
        "arc_fuses": arcs,
    }
    Path(out).write_text(json.dumps(data, indent=1) + "\n")
    return data


def _profile_key(tile: BfdTile) -> str:
    import hashlib
    payload = json.dumps({f"{a}->{b}": sorted({f['fuse'] for f in v})
                          for (a, b), v in tile.arcs().items()}, sort_keys=True)
    return hashlib.md5(payload.encode()).hexdigest()


def export_profiles(tiles: dict[str, BfdTile], cal: Calibration,
                    out: Path, min_cols: int = 48, max_cols: int = 48) -> dict:
    """Export one routing table per distinct 29-row CIB profile.

    Tile types with identical arc->fuse maps share a profile, so the complete
    CIB/long-wire map fits in a handful of tables instead of one per type.
    Only the 29x48 CIB family (PLC, CIB_CFG*, CIB_DSP*, CIB_EBR*) is included;
    the 59-column PIC/LLC/ULC tiles use a wider Ram grid and are not exported.
    """
    groups: dict[str, list[str]] = {}
    for name, t in sorted(tiles.items()):
        if t.rows != TILE_ROWS or not (min_cols <= t.cols <= max_cols):
            continue
        if not t.arc_set():
            continue
        groups.setdefault(_profile_key(t), []).append(name)
    profiles: dict[str, dict] = {}
    union: set[str] = set()
    for key, names in groups.items():
        t = tiles[names[0]]
        data = export(t, cal, Path("/dev/null"))
        data.pop("device", None)
        data.pop("source", None)
        data["tile_types"] = names
        label = names[0] if len(names) == 1 else names[0].rsplit("0", 1)[0]
        profiles[label] = data
        union |= set(data["arc_fuses"])
    all_types = [n for names in groups.values() for n in names]
    long_wire = {a for a in union
                 if any(__import__("re").match(r"^[VH]\d{2}[NSEW]", w)
                        for w in a.split("->"))}
    data = {
        "device": "SL2-12E-8F256C",
        "source": "vendor BFD dump ('bfd.dumpbfd), parsed by xist.bfd",
        "note": ("Complete routing fuse map for the 29-row CIB tile types. "
                 "Tile types with identical maps share a profile.  Bit "
                 "coordinates use column_base from SL2-12E_bitcol_base.json "
                 "(validated on PLC only)."),
        "row_rule": "bit_row = 29 * (tile_row - 1) + ram_row",
        "col_rule": "bit_col = column_base[tile_col] - ram_col",
        "profiles": profiles,
        "coverage": {
            "tile_types": all_types,
            "arcs_union": len(union),
            "long_wire_arcs_union": len(long_wire),
        },
    }
    Path(out).write_text(json.dumps(data, separators=(",", ":")) + "\n")
    return data


def validate_dir(workdir: str | Path, tiles: dict[str, BfdTile],
                 cal: Calibration, tile_type: str = "PLC",
                 limit: int = 0) -> dict:
    """Check BFD routing-fuse predictions against vendor bitstreams.

    *workdir* must contain paired ``R<row>C<col>_*.xpn`` / ``.bit`` files.
    Returns counts of designs and fuse-bit matches.
    """
    import glob
    import os
    import re
    from .bitstream import Bitstream
    from .program import routing_equations, used_arcs

    tile = tiles[tile_type]
    req = routing_equations(tile)
    designs = 0
    perfect = 0
    total = match = pred1 = 0
    for xpn in sorted(glob.glob(str(Path(workdir) / "*.xpn"))):
        bit = xpn[:-4] + ".bit"
        m = re.match(r"R(\d+)C(\d+)_", os.path.basename(xpn))
        if not m or not os.path.exists(bit):
            continue
        tr, tc = int(m.group(1)), int(m.group(2))
        if tc not in cal.column_base:
            continue
        if limit and designs >= limit:
            break
        used = used_arcs(xpn, tr, tc)
        bs = Bitstream.read(bit)
        designs += 1
        bad = 0
        for arcs, positions in req.values():
            val = not arcs.isdisjoint(used)
            for (rr, rc) in positions:
                br, bc = cal.bit(tr, tc, rr, rc)
                total += 1
                if val:
                    pred1 += 1
                if bs.rows[br][bc] == ("1" if val else "0"):
                    match += 1
                else:
                    bad += 1
        perfect += (bad == 0)
    return dict(designs=designs, perfect=perfect, positions=total,
                match=match, predicted_one=pred1)


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="BFD -> absolute routing fuse map")
    ap.add_argument("bfd")
    ap.add_argument("--tile", default="PLC")
    ap.add_argument("--out", default=None)
    ap.add_argument("--calibration", default=None,
                    help="bitcol base JSON (default: testdata)")
    ap.add_argument("--calibrate", action="store_true",
                    help="build the calibration from columns.json")
    ap.add_argument("--profiles", action="store_true",
                    help="export one table per distinct CIB profile")
    ap.add_argument("--validate", nargs="+", default=None,
                    help="one or more directories of paired .xpn/.bit files")
    args = ap.parse_args(argv)
    tiles = parse(args.bfd)
    if args.calibration:
        cal = load_calibration(args.calibration)
    elif args.calibrate:
        columns = json.loads((TESTDATA / "SL2-12E_columns.json").read_text())
        cal = calibration_from_columns(columns)
    else:
        cal = load_calibration()
    if args.profiles:
        out = args.out or str(TESTDATA / "SL2-12E_bfd_profiles.json")
        d = export_profiles(tiles, cal, Path(out))
        print(f"wrote {out}: {len(d['profiles'])} profiles, "
              f"{len(d['coverage']['tile_types'])} tile types, "
              f"{d['coverage']['arcs_union']} arcs "
              f"({d['coverage']['long_wire_arcs_union']} long-wire)")
        return 0
    if args.validate:
        agg = dict(designs=0, perfect=0, positions=0, match=0, predicted_one=0)
        for d in args.validate:
            r = validate_dir(d, tiles, cal, args.tile)
            for k in agg:
                agg[k] += r[k]
        pct = 100.0 * agg["match"] / agg["positions"] if agg["positions"] else 0.0
        print(f"designs {agg['designs']} perfect {agg['perfect']} "
              f"positions {agg['positions']} match {agg['match']} "
              f"({pct:.3f}%) predicted-one {agg['predicted_one']}")
        return 0 if agg["designs"] and agg["match"] == agg["positions"] else 1
    t = tiles.get(args.tile)
    if not t:
        print(f"no tile {args.tile!r}")
        return 1
    print(f"{t.name}: {t.rows}x{t.cols}, {len(t.ram)} ram rows, "
          f"{len(t.equations)} equations, {len(t.arc_set())} arcs, "
          f"{len(t.arcs())} arcs with a fuse")
    if args.out:
        d = export(t, cal, Path(args.out))
        print(f"wrote {args.out}: {len(d['fuses'])} fuses, "
              f"{d['arcs']} arcs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
