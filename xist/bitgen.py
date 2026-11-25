"""Write a `.bit` from a nextpnr routed JSON netlist.

This is the bridge the bring-up flow was missing: `xist/build.py` replays the
vendor XPN, this module consumes what nextpnr actually produced.

Inputs:

* ``routed.json``  - ``--write`` output of nextpnr-himbaechel.  Cell placement
  is in ``NEXTPNR_BEL`` (``X2Y2/LUT0``), routes are ``ROUTING`` attributes in
  nextpnr's ``wire;pip;strength`` triplets.  A pip is named
  ``X2Y2/DI0/F0`` = ``tile/dst/src``, so it maps directly to a BFD arc
  ``F0 -> DI0``.
* a template ``.bit`` (only the device header/all-ones background are reused;
  every modelled fuse is rewritten).
* the BFD profile files in ``testdata`` (same encoders as ``build.py``).

What is written:

* PLC routing muxes from the routed pips (``program_plc_used``), and the same
  for the other modelled tile contexts;
* one synthesised slice program per used slice (``xist/sliceprog.py``,
  including reset/CE), evaluated by ``program_slice_comps``;
* LUT INIT from the routed LUT cells' ``INIT`` parameter;
* the checksum.

PIO configuration is not written yet (``apply_pio`` still needs the XPN
``IOBUF`` properties), so pads keep the template's configuration.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from .bfdmap import load_calibration
from .bitstream import Bitstream
from .apply import PROFILE, apply_pio_comps, profile_config_equations
from .build import (TESTDATA, _set, program_lut_inits, program_plc_used,
                     program_plc_rows, program_slice_comps, site_of)
from .chipdb import _context_name, default_bfd_profiles
from .device import parse_udev_report
from .sliceprog import SliceFacts, slice_program
from .piogen import iologic_comps, pio_comps
from .xpn import Comp

_XY = re.compile(r"X(\d+)Y(\d+)")
_BEL = re.compile(r"(LUT|DFF)(\d)$")
SLICE_LETTERS = "ABCD"


def _top_module(routed: dict) -> dict:
    modules = routed["modules"]
    for module in modules.values():
        if "top" in module.get("attributes", {}):
            return module
    return next(iter(modules.values()))


def routed_pips(routed: dict) -> dict[tuple[int, int], set[tuple[str, str]]]:
    """Per (row, col) BFD arcs ``(src, dst)`` used by the routed nets.

    nextpnr names a pip ``tile/dst/src``; ``X`` is the column and ``Y`` the
    row, matching the XPN's ``R<row>C<col>`` convention.
    """
    out: dict[tuple[int, int], set[tuple[str, str]]] = {}
    for net in _top_module(routed)["netnames"].values():
        routing = net.get("attributes", {}).get("ROUTING")
        if not routing:
            continue
        segs = routing.split(";")
        for i in range(0, len(segs) - 2, 3):
            pip = segs[i + 1]
            if not pip:
                continue
            parts = pip.split("/")
            if len(parts) != 3:
                continue            # tile/dst/src/INV or a synthetic pip
            tile, dst, src = parts
            m = _XY.match(tile)
            if not m:
                continue
            out.setdefault((int(m.group(2)), int(m.group(1))), set()).add((src, dst))
    return out


def routed_cells(routed: dict) -> dict[tuple[int, int],
                                      dict[str, dict[int, dict]]]:
    """Per (row, col) and slice letter, the used LUT/DFF banks.

    Result: ``{tile: {letter: {"luts": set[int], "dffs": set[int],
    "inits": {bank: int}, "reset": bool, "ce": bool}}}``.
    """
    out: dict[tuple[int, int], dict[str, dict]] = {}
    for cell in _top_module(routed)["cells"].values():
        typ = cell["type"]
        bel = cell.get("attributes", {}).get("NEXTPNR_BEL")
        if not bel or typ not in ("LUT4", "DFF"):
            continue
        m = _XY.match(bel)
        if not m:
            continue
        row, col = int(m.group(2)), int(m.group(1))
        bm = _BEL.search(bel.split("/")[1])
        if not bm:
            continue
        bank = int(bm.group(2))
        letter = SLICE_LETTERS[bank // 2]
        slot = out.setdefault((row, col), {}).setdefault(
            letter, {"luts": set(), "dffs": set(), "inits": {},
                     "reset": False, "ce": False, "reset_async": False})
        if typ == "LUT4":
            slot["luts"].add(bank % 2)
            init = cell.get("parameters", {}).get("INIT", "0")
            slot["inits"][bank % 2] = int(init, 2) if isinstance(init, str) else int(init)
        else:
            dff_bank = bank % 2
            slot["dffs"].add(dff_bank)
            params = cell.get("parameters", {})
            conns = cell.get("connections", {})
            slot["reset"] = slot["reset"] or bool(conns.get("R")) or bool(conns.get("S"))
            slot["ce"] = slot["ce"] or bool(conns.get("CE"))
            # DFFR/DFFS are synchronous, DFFC/DFFP asynchronous; the converter
            # records which in RESET_ASYNC.
            if slot["reset"]:
                slot["reset_async"] = slot.get("reset_async") or \
                    params.get("RESET_ASYNC", "1") != "0"
    return out


def slice_facts(slot: dict) -> SliceFacts:
    return SliceFacts(luts=tuple(sorted(slot["luts"])),
                      dffs=tuple(sorted(slot["dffs"])),
                      sd_from_lut=True,
                      reset=slot["reset"],
                      reset_is_async=slot.get("reset_async", True),
                      ce=slot["ce"])


def write_bitstream(base: str | Path, out: str | Path, *,
                    routed: dict | None = None,
                    pips: dict | None = None,
                    cells: dict | None = None,
                    all_tiles: list[tuple[int, int]] | None = None,
                    verbose: bool = True) -> dict:
    """Assemble a .bit from routed data.

    ``routed`` is a nextpnr JSON; ``pips``/``cells`` may be supplied directly
    (already in the shapes returned by :func:`routed_pips` /
    :func:`routed_cells`) for tests.
    """
    cal = load_calibration()
    profile_path = TESTDATA / "SL2-12E_bfd_profiles.json"
    profiles = json.loads(profile_path.read_text())["profiles"]
    plc_profile = json.loads((TESTDATA / "SL2-12E_bfd_PLC.json").read_text())
    slice_json = json.loads((TESTDATA / "SL2-12E_bfd_slice.json").read_text())

    if routed is not None:
        pips = routed_pips(routed)
        cells = routed_cells(routed)
    pips = pips or {}
    cells = cells or {}

    rep = parse_udev_report((TESTDATA / "SL2-12E-8F256C_udev.txt").read_text())
    if all_tiles is None:
        all_tiles = [(r, c) for r, row in enumerate(rep.tiles)
                     for c, tok in enumerate(row)
                     if tok and tok != "."]

    ctx_profile = {}
    for ctx, (path, name) in default_bfd_profiles().items():
        data = json.loads(Path(path).read_text())
        ctx_profile[ctx] = data["profiles"][name] if name else data

    bs = Bitstream.read(str(base))
    nbits = 0
    unmodelled = set()
    for row, col in all_tiles:
        if col not in cal.column_base:
            continue
        token = rep.tiles[row][col]
        ctx = _context_name(token, row, col)
        prof = ctx_profile.get(ctx)
        if prof is None:
            unmodelled.add(ctx)
            continue
        nbits += program_plc_used(bs, prof, {}, cal, row, col,
                                  set(pips.get((row, col), set())))
        if ctx == "PLC":
            nbits += program_slice_comps(bs, _tile_comps(row, col, cells),
                                          slice_json, cal, row, col)
            nbits += program_lut_inits(bs, plc_profile.get("lut_init", {}),
                                       cal, row, col,
                                       _tile_inits(row, col, cells))

    # top/bottom pad blocks are their own 6-row instances
    for row, base_row in ((0, 1108), (39, 1102)):
        for col, tok in enumerate(rep.tiles[row]):
            if not tok or tok == "." or col not in cal.column_base:
                continue
            ctx = _context_name(tok, row, col)
            prof = ctx_profile.get(ctx)
            if prof is None:
                unmodelled.add(ctx)
                continue
            nbits += program_plc_rows(bs, None, prof, cal, row, col,
                                      base_row,
                                      used=pips.get((row, col), set()))

    pio_list = pio_comps(routed) if routed is not None else []
    pio_list += iologic_comps(routed) if routed is not None else []
    edge_eqs = {side: profile_config_equations(profiles[PROFILE[side]])
                for side in "LRTB"}
    npio = apply_pio_comps(bs, pio_list, calibration=cal,
                           profile_eqs=edge_eqs)
    nbits += npio

    bs.update_checksum()
    bs.write(str(out))
    result = dict(bits=nbits, pio=npio, checksum=bs.extra16,
                  unmodelled=sorted(unmodelled),
                  tiles=len(all_tiles))
    if verbose:
        print(f"wrote {out}: {nbits} fuse bits, checksum {bs.extra16}")
        if unmodelled:
            print(f"  unmodelled tile contexts (left at template): "
                  f"{', '.join(sorted(unmodelled))}")
    return result


def _tile_comps(row: int, col: int, cells: dict) -> list[Comp]:
    comps = []
    for letter, slot in (cells.get((row, col)) or {}).items():
        if not slot["luts"] and not slot["dffs"]:
            continue
        site = f"R{row}C{col}{letter}"
        comps.append(Comp(name=f"SLICE_{letter}", site=site,
                          program=slice_program(slice_facts(slot))))
    return comps


def _tile_inits(row: int, col: int, cells: dict) -> dict[str, dict[int, int]]:
    """INIT for both banks of every *instantiated* slice.

    The vendor writes INIT 0 (bitstream char '1') for the unused bank of a
    slice that exists, but leaves entirely uninstantiated slices at 0, so they
    must not be touched.
    """
    out: dict[str, dict[int, int]] = {}
    for letter, slot in (cells.get((row, col)) or {}).items():
        if not slot["luts"] and not slot["dffs"]:
            continue
        inits = dict(slot["inits"])
        out[f"SLICE_{letter}"] = {bank: inits.get(bank, 0) for bank in (0, 1)}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="assemble a .bit from routed JSON")
    ap.add_argument("--base", required=True, help="template .bit")
    ap.add_argument("--routed", required=True, help="nextpnr --write JSON")
    ap.add_argument("--out", required=True)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)
    routed = json.loads(Path(args.routed).read_text())
    res = write_bitstream(args.base, args.out, routed=routed,
                          verbose=not args.quiet)
    return 0 if res else 1


if __name__ == "__main__":
    raise SystemExit(main())
