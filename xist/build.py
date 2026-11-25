"""Assemble a .bit from an XPN: routing + slice config + PIO + checksum.

This is the Python prototype of the uarch bitstream writer.  It starts from a
template `.bit` and overlays the regions we can compute from the routed XPN:

* PLC routing muxes (BFD profile `SL2-12E_bfd_PLC.json`)
* PLC slice (DFF/ALU/LUT-control) configuration (`SL2-12E_bfd_slice.json`)
* edge PIO configuration (`SL2-12E_bfd_pio.json`)

LUT INIT and any residual tile types are not written yet.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .apply import apply_pio
from .bfd_expr import evaluate, parse_cached
from .bfdmap import load_calibration
from .bitstream import Bitstream
from .program import _set, used_arcs
from .sliceconfig import env_for
from .xpn import parse_comps

HERE = Path(__file__).resolve().parent
TESTDATA = HERE / "testdata"


def _is_config_fuse(profile: dict, fuse: str) -> bool:
    """True when the fuse is not a plain positive routing mux.

    Covers PIO/IOLOGIC ``config()``/``property()`` rules, ``FALSE`` placeholders
    and the inverted-guarded form ``!arcval(a,b) && wire(w)`` (e.g. the PIO
    input-delay INDEL).  Those are written by the PIO path or left alone; the
    routing path must not drive them with a positive-arc value.
    """
    expr = (profile.get("fuse_exprs") or {}).get(fuse, "")
    if "config(" in expr or "property(" in expr:
        return True
    if not expr.strip() or expr.strip().upper() == "FALSE":
        return True
    if "!arcval" in expr or "wire(" in expr:
        return True
    return False


def _fuse_arcs(profile: dict) -> dict[str, set[tuple[str, str]]]:
    out: dict[str, set[tuple[str, str]]] = {}
    for arc, names in profile["arc_fuses"].items():
        src, dst = arc.split("->", 1)
        for name in names:
            out.setdefault(name, set()).add((src, dst))
    return out


def program_plc(bs: Bitstream, xpn_path, profile: dict, aliases: dict,
                cal, tile_row: int, tile_col: int) -> int:
    used = used_arcs(xpn_path, tile_row, tile_col)
    amap = dict(aliases or {})
    amap.update(profile.get("aliases") or {})
    used = {(amap.get(a, a), amap.get(b, b)) for a, b in used}
    n = 0
    for fuse, arcs in _fuse_arcs(profile).items():
        if _is_config_fuse(profile, fuse):
            continue
        val = not arcs.isdisjoint(used)
        for (rr, rc) in profile["fuses"][fuse]:
            br, bc = cal.bit(tile_row, tile_col, rr, rc)
            if 0 <= br < bs.nrows and 0 <= bc < bs.ncols:
                _set(bs, br, bc, val)
                n += 1
    return n


VARMAP = {"A": 0, "B": 1, "C": 2, "D": 3}


def _site_refs(node, out=None) -> set[str]:
    """Collect the SLICE_x site names referenced by a parsed expression."""
    if out is None:
        out = set()
    if isinstance(node, tuple):
        if node and node[0] == "pred":
            for arg in node[2]:
                if isinstance(arg, str) and arg.startswith("SLICE_"):
                    out.add(arg)
        else:
            for part in node[1:]:
                _site_refs(part, out)
    return out


def program_luts(bs: Bitstream, xpn_path, lut_init: dict, cal,
                 tile_row: int, tile_col: int) -> int:
    """Program LUT INIT from the XPN slice function (K0 -> bank 0, K1 -> bank 1)."""
    from .lutinit import init_from_function
    from .sliceconfig import parse_slice_program, slice_site
    n = 0
    for comp in parse_comps(xpn_path).values():
        site = slice_site(comp.site)
        if site is None or site not in lut_init:
            continue
        m = __import__("re").match(r"R(\d+)C(\d+)([A-D])$", comp.site)
        if not m or (int(m.group(1)), int(m.group(2))) != (tile_row, tile_col):
            continue
        _props, _cfgs, funcs = parse_slice_program(comp.program)
        for bank, key in ((0, "K0"), (1, "K1")):
            expr = funcs.get(key)
            bank_map = lut_init[site].get(str(bank)) or lut_init[site].get(bank)
            if not expr or bank_map is None:
                continue
            init = init_from_function(expr, VARMAP)
            for j, (rr, rc) in enumerate(bank_map):
                if rr is None:
                    continue
                br, bc = cal.bit(tile_row, tile_col, rr, rc)
                if 0 <= br < bs.nrows and 0 <= bc < bs.ncols:
                    _set(bs, br, bc, not ((init >> j) & 1))
                    n += 1
    return n


def program_plc_rows(bs: Bitstream, xpn_path, profile: dict, cal,
                     tile_row: int, tile_col: int, base_row: int) -> int:
    """Same as program_plc() but for the 6-row top/bottom blocks."""
    used = used_arcs(xpn_path, tile_row, tile_col)
    amap = dict(profile.get("aliases") or {})
    used = {(amap.get(a, a), amap.get(b, b)) for a, b in used}
    n = 0
    for fuse, arcs in _fuse_arcs(profile).items():
        if _is_config_fuse(profile, fuse):
            continue
        val = not arcs.isdisjoint(used)
        for (rr, rc) in profile["fuses"][fuse]:
            br = base_row + rr
            bc = cal.column_base[tile_col] - rc
            if 0 <= br < bs.nrows and 0 <= bc < bs.ncols:
                _set(bs, br, bc, val)
                n += 1
    return n


def program_slice(bs: Bitstream, xpn_path, slice_json: dict, cal,
                  tile_row: int, tile_col: int) -> int:
    eqs = [(name, parse_cached(info["expr"]),
            [tuple(p) for p in info["positions"]])
           for name, info in slice_json["fuses"].items()]
    comps = []
    for comp in parse_comps(xpn_path).values():
        site = comp.site
        if not site or not site.startswith("R"):
            continue
        if site[1:site.index("C")] != str(tile_row):
            continue
        if int(site[site.index("C") + 1:-1]) != tile_col:
            continue
        comps.append(comp)
    env = env_for(comps)
    n = 0
    for _name, ast, positions in eqs:
        # only touch slices that are actually instantiated in this tile; the
        # vendor keeps the sibling slices at their own defaults
        sites = _site_refs(ast)
        if sites and not (sites & env["sites"]):
            continue
        value = evaluate(ast, env)
        for (rr, rc) in positions:
            br, bc = cal.bit(tile_row, tile_col, rr, rc)
            if 0 <= br < bs.nrows and 0 <= bc < bs.ncols:
                _set(bs, br, bc, value)
                n += 1
    return n


def build(base: str | Path, xpn_path: str | Path, out: str | Path,
          plc_profile=None, slice_json=None, pio=True,
          all_tiles=None) -> dict:
    cal = load_calibration()
    plc_profile = plc_profile or json.loads(
        (TESTDATA / "SL2-12E_bfd_PLC.json").read_text())
    slice_json = slice_json or json.loads(
        (TESTDATA / "SL2-12E_bfd_slice.json").read_text())
    aliases = json.loads((TESTDATA / "SL2-12E_bfd_aliases.json").read_text())["PLC"]
    bs = Bitstream.read(str(base))
    nr = 0
    for comp in parse_comps(xpn_path).values():
        site = comp.site or ""
        if not site.startswith("R") or "_" not in site:
            continue
        # plumbing tiles are not slice sites; PLC tiles come from the route
    # program every PLC tile that appears in the route
    from .chipdb import _context_name, default_bfd_profiles
    from .device import parse_udev_report
    from .xpn import wire_tile

    rep = parse_udev_report(
        (TESTDATA / "SL2-12E-8F256C_udev.txt").read_text())
    if all_tiles is None:
        all_tiles = [(r, c) for r, row in enumerate(rep.tiles)
                     for c, tok in enumerate(row)
                     if tok and tok != "."]
    ctx_profile = {}
    for ctx, (path, name) in default_bfd_profiles().items():
        data = json.loads(Path(path).read_text())
        ctx_profile[ctx] = data["profiles"][name] if name else data

    # a real writer must drive every tile, including the unused ones (their
    # fuses must be 0, otherwise a stale template value survives)
    tiles = set()
    for (row, col) in all_tiles:
        if col in cal.column_base:
            tiles.add((row, col))
    # top and bottom pad blocks live outside the 29-row pitch
    edge_tiles = []
    for row, token in ((0, "PIC_top"), (39, "PIC_bot")):
        for col, tok in enumerate(rep.tiles[row]):
            if tok and tok != "." and ctx_profile.get(_context_name(tok, row, col)):
                edge_tiles.append((row, col))

    for (row, col) in tiles:
        if col not in cal.column_base:
            continue
        token = rep.tiles[row][col] if row < len(rep.tiles) else "."
        ctx = _context_name(token, row, col)
        prof = ctx_profile.get(ctx)
        if prof is None:
            # unmodelled context: leave its fuses at the template value rather
            # than writing a wrong profile
            continue
        nr += program_plc(bs, xpn_path, prof, aliases, cal, row, col)
        nr += program_slice(bs, xpn_path, slice_json, cal, row, col)
        nr += program_luts(bs, xpn_path, prof.get("lut_init", {}),
                           cal, row, col)

    for (row, col) in edge_tiles:
        if col not in cal.column_base:
            continue
        token = rep.tiles[row][col]
        ctx = _context_name(token, row, col)
        prof = ctx_profile.get(ctx)
        if prof is None:
            continue
        base_row = 1108 if row == 0 else 1102
        # the pad blocks are their own tile instance; used_arcs must look at the
        # XPN route endpoints that belong to this column, not the 29-row tile
        nr += program_plc_rows(bs, xpn_path, prof, cal, row, col, base_row)
    npio = apply_pio(bs, xpn_path) if pio else 0
    bs.update_checksum()
    Path(out).write_text("")  # placeholder; write below
    bs.write(str(out))
    return dict(plc=nr, pio=npio, checksum=bs.extra16)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="assemble a .bit from an XPN")
    ap.add_argument("--base", required=True)
    ap.add_argument("--xpn", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--no-pio", action="store_true")
    args = ap.parse_args(argv)
    r = build(args.base, args.xpn, args.out, pio=not args.no_pio)
    print(f"programmed {r['plc']} PLC config bits, {r['pio']} PIO bits; checksum {r['checksum']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
