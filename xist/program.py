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
from .xpn import parse_xpn, wire_local


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


def used_arcs(path: str | Path, tile_row: int, tile_col: int,
              tile=None) -> set[tuple[str, str]]:
    """Tile-local routing arcs from an XPN, oriented as in the BFD.

    The BFD equation is ``fuse = arcval(source, destination)``.  The XPN route
    line is ``destination.source``; ``xist.xpn`` stores it as ``(source,
    destination)``, so the BFD arc for a pip is ``(dst_local, src_local)``.

    XPN wires may use the BFD node aliases (``R26C1_JA2`` for the canonical
    ``A2``), so pass *tile* to translate them through ``tile.aliases``.
    """
    prefix = f"R{tile_row}C{tile_col}_"
    alias = tile.aliases if tile is not None else {}
    used = set()
    for sig in parse_xpn(path).values():
        for src, dst in sig.route:
            s_in = src.startswith(prefix)
            d_in = dst.startswith(prefix)
            if u_in := (s_in or d_in):
                pass
            if not u_in:
                continue
            # A long-wire hop is written as a cross-tile pair, e.g.
            # R2C19_H06E0200.R5C19_H06E0206: the local wires of both ends are
            # active in their own tiles, so keep the endpoint that is local.
            a = wire_local(dst) if d_in else None
            b = wire_local(src) if s_in else None
            if a is not None and b is not None:
                used.add((alias.get(a, a), alias.get(b, b)))
            elif a is not None:
                used.add((alias.get(a, a), a))
            elif b is not None:
                used.add((b, alias.get(b, b)))
    return used


def used_wires(path: str | Path, tile_row: int, tile_col: int,
               tile=None) -> set[str]:
    """Local wires that appear in the tile's routed pips (aliases resolved)."""
    prefix = f"R{tile_row}C{tile_col}_"
    alias = tile.aliases if tile is not None else {}
    out = set()
    for sig in parse_xpn(path).values():
        for src, dst in sig.route:
            if src.startswith(prefix) and dst.startswith(prefix):
                for w in (wire_local(src), wire_local(dst)):
                    out.add(alias.get(w, w))
    return out


def routing_equations(tile) -> dict[str, tuple[set[tuple[str, str]], list]]:
    """Fuses driven only by routing arcs -> (arcs, ram positions)."""
    from .bfd import _ARC_RE
    special = tile.special_fuses()
    out = {}
    for name, expr in tile.equations.items():
        if name in special:
            continue
        if "arcval" not in expr or "config(" in expr or "property(" in expr:
            continue
        arcs = {(m.group(1).strip(), m.group(2).strip())
                for m in _ARC_RE.finditer(expr)}
        out[name] = (arcs, tile.fuse_positions(name))
    return out


def program_routing(bs: Bitstream, xpn_path: str | Path, tile, cal,
                    tile_row: int, tile_col: int) -> int:
    """Program every routing fuse of one tile from the tile's XPN arcs.

    The fuse is set when any of its arcs is used and cleared otherwise.  This
    reproduces the vendor bitstream for the routing region (validated end to
    end, see :mod:`xist.bfdmap`).
    """
    used = used_arcs(xpn_path, tile_row, tile_col, tile)
    wires = used_wires(xpn_path, tile_row, tile_col, tile)
    n = 0
    for arcs, positions in routing_equations(tile).values():
        val = not arcs.isdisjoint(used)
        for (rr, rc) in positions:
            br, bc = cal.bit(tile_row, tile_col, rr, rc)
            if 0 <= br < bs.nrows and 0 <= bc < bs.ncols:
                _set(bs, br, bc, val)
                n += 1
    # inverted + wire-guarded fuses: fuse=1 iff the arc is unused and the
    # guarded wire is active (e.g. INDEL = !arcval(PIO,JDI) && wire(JDI))
    for name, meta in tile.special_fuses().items():
        a, b = meta["arc"]
        guard = tile.aliases.get(meta["guard"], meta["guard"])
        arc_used = (a, b) in used
        val = (not arc_used) and (guard in wires)
        if meta.get("invert"):
            pass
        else:
            val = not val
        for (rr, rc) in tile.fuse_positions(name):
            br, bc = cal.bit(tile_row, tile_col, rr, rc)
            if 0 <= br < bs.nrows and 0 <= bc < bs.ncols:
                _set(bs, br, bc, val)
                n += 1
    return n


def program_pio(bs: Bitstream, xpn_path: str | Path, equations,
                tile_row: int, col_base: int, side: str,
                row_base: int | None = None) -> int:
    """Program one edge tile's PIO configuration from the XPN component data."""
    from .pioconfig import bits_from_equations
    from .xpn import parse_comps
    comps = []
    for comp in parse_comps(xpn_path).values():
        site = comp.site
        if not site or len(site) < 4 or site[0] != "P" or site[1] != side:
            continue
        if not site[2:-1].isdigit() or int(site[2:-1]) != tile_row:
            continue
        comps.append(comp)
    n = 0
    for (br, bc, val) in bits_from_equations(equations, comps, tile_row,
                                              col_base, row_base):
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
    bs.update_checksum()
    bs.write(args.out)
    print(f"programmed {n} fuses -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
