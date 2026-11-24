"""Build a nextpnr-himbaechel chip database skeleton from XiST vendor data.

Sources
-------
* tile grid / sites / resource counts: ``'udev.report`` (see :mod:`xist.device`)
* package pins: ``'udev.pkg.report``
* routing samples: routed ``.xpn`` netlists (:mod:`xist.xpn`)
* LUT INIT fuse maps: differential probes (:mod:`xist.lutmap`)

The routing graph produced from a corpus of XPN files is necessarily partial:
it only contains wires and muxes that the vendor router actually used.  It is
enough to validate the database format and to start place-and-route work, but a
production chipdb still needs systematic pip fuzzing.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .dbgen.chip import Chip, NodeWire, PinType
from .device import DeviceReport, family_for
from .lutmap import LutCalibration
from .xpn import routing_edges, wire_local, wire_tile

SLICE_LETTERS = "ABCD"


@dataclass
class XistChip:
    chip: Chip
    part: str
    family: str
    luts: dict[str, LutCalibration] = field(default_factory=dict)
    columns: dict[int, int | None] | None = None

    def write_bba(self, path: str | Path) -> None:
        self.chip.write_bba(str(path))

    def write_meta(self, path: str | Path) -> None:
        """Non-bba data nextpnr's bitstream writer needs (fuse maps)."""
        meta = {
            "part": self.part,
            "family": self.family,
            "lut": {k: v.to_json() for k, v in self.luts.items()},
            "column_origin": ({str(k): v for k, v in self.columns.items()}
                              if self.columns else None),
        }
        Path(path).write_text(json.dumps(meta, indent=2) + "\n")


def _add_plc_tile_type(chip: Chip, name: str) -> None:
    tt = chip.create_tile_type(name)
    # clock/reset are shared by all four slices; create them exactly once so the
    # bel pins and the CIB pips refer to the same wire index
    for w in ("CLK", "LSR"):
        tt.create_wire(w)
    for s in range(4):
        n0, n1 = 2 * s, 2 * s + 1
        # Wire names follow the vendor XPN conventions: slice A uses F0/F1,
        # B uses F2/F3, C uses F4/F5, D uses F6/F7 (and likewise A0..D0/A1..D1,
        # DI0/DI1, Q0/Q1).  Sharing these names lets the pips observed in XPN
        # dumps connect directly to the bel pins.
        for w in (f"A{n0}", f"B{n0}", f"C{n0}", f"D{n0}",
                  f"A{n1}", f"B{n1}", f"C{n1}", f"D{n1}",
                  f"F{n0}", f"F{n1}", f"Q{n0}", f"Q{n1}",
                  f"DI{n0}", f"DI{n1}"):
            tt.create_wire(w)
        l0 = tt.create_bel(f"LUT{n0}", "LUT4", 4 * s)
        l1 = tt.create_bel(f"LUT{n1}", "LUT4", 4 * s + 1)
        d0 = tt.create_bel(f"DFF{n0}", "DFF", 4 * s + 2)
        d1 = tt.create_bel(f"DFF{n1}", "DFF", 4 * s + 3)
        for lut, n, out in ((l0, n0, n0), (l1, n1, n1)):
            for pin, w in zip(("I[0]", "I[1]", "I[2]", "I[3]"),
                              (f"A{n}", f"B{n}", f"C{n}", f"D{n}")):
                tt.add_bel_pin(lut, pin, w, PinType.INPUT)
            tt.add_bel_pin(lut, "F", f"F{out}", PinType.OUTPUT)
        for dff, n in ((d0, n0), (d1, n1)):
            tt.add_bel_pin(dff, "D", f"DI{n}", PinType.INPUT)
            tt.add_bel_pin(dff, "CLK", "CLK", PinType.INPUT)
            tt.add_bel_pin(dff, "Q", f"Q{n}", PinType.OUTPUT)
        # Carry/math cell: one ALU per slice, sharing the slice data inputs.
        for w in (f"CIN{n0}", f"COUT{n0}", f"SUM{n0}",
                  f"AI0_{n0}", f"AI1_{n0}", f"AI2_{n0}", f"AI3_{n0}"):
            tt.create_wire(w)
        alu = tt.create_bel(f"ALU{n0}", "ALU", 16 + s)
        tt.add_bel_pin(alu, "I0", f"AI0_{n0}", PinType.INPUT)
        tt.add_bel_pin(alu, "I1", f"AI1_{n0}", PinType.INPUT)
        tt.add_bel_pin(alu, "I2", f"AI2_{n0}", PinType.INPUT)
        tt.add_bel_pin(alu, "I3", f"AI3_{n0}", PinType.INPUT)
        tt.add_bel_pin(alu, "CIN", f"CIN{n0}", PinType.INPUT)
        tt.add_bel_pin(alu, "COUT", f"COUT{n0}", PinType.OUTPUT)
        tt.add_bel_pin(alu, "SUM", f"SUM{n0}", PinType.OUTPUT)
        # LUT output can feed the slice DFF and the fabric output
        tt.create_pip(f"F{n0}", f"DI{n0}")
        tt.create_pip(f"F{n1}", f"DI{n1}")
        g = tt.create_group(f"SLICE{s}", "SLICE")
        for bel in (l0, l1, d0, d1):
            tt.add_bel_to_group(bel, f"SLICE{s}")


def _add_io_tile_type(chip: Chip, name: str, n_io: int = 4) -> None:
    tt = chip.create_tile_type(name)
    for s in range(n_io):
        slot = SLICE_LETTERS[s]
        # Vendor XPN names: input data JDIx, input mux JPADDIx_PIO,
        # fabric->pad mux JPADDOx, pad data PADDOx_PIO.
        for w in (f"PAD_{slot}", f"JDI{slot}", f"JPADDI{slot}_PIO",
                  f"JPADDO{slot}", f"PADDO{slot}_PIO", f"OE_{slot}"):
            tt.create_wire(w)
        b = tt.create_bel(f"PIO_{slot}", "PIO", s)
        tt.add_bel_pin(b, "PAD", f"PAD_{slot}", PinType.INOUT)
        tt.add_bel_pin(b, "PADDI", f"JDI{slot}", PinType.OUTPUT)
        tt.add_bel_pin(b, "PADDO", f"PADDO{slot}_PIO", PinType.INPUT)
        tt.add_bel_pin(b, "OE", f"OE_{slot}", PinType.INPUT)
        tt.create_pip(f"JDI{slot}", f"JPADDI{slot}_PIO")
        tt.create_pip(f"JPADDO{slot}", f"PADDO{slot}_PIO")


def _add_hard_tile_type(chip: Chip, name: str) -> None:
    tt = chip.create_tile_type(name)
    # placeholders: real pin maps come from the (encrypted) device database
    tt.create_wire(f"{name}_IN")
    tt.create_wire(f"{name}_OUT")
    b = tt.create_bel(f"{name}0", name, 0)
    tt.add_bel_pin(b, "IN", f"{name}_IN", PinType.INPUT)
    tt.add_bel_pin(b, "OUT", f"{name}_OUT", PinType.OUTPUT)


def _tile_kind(token: str) -> str:
    if token.startswith("PLC"):
        return "PLC"
    if token.startswith("PIC"):
        return "PIC"
    if token.startswith("EBR"):
        return "EBR"
    if token.startswith("DSP"):
        return "DSP"
    return "HARD"


def build_chip(part: str, report: DeviceReport,
               luts: dict[str, LutCalibration] | None = None,
               xpn_paths: list[str] | None = None,
               pkg_pins: list[dict] | None = None,
               columns: dict[int, int | None] | None = None,
               constids_path: str | Path | None = None) -> XistChip:
    chip = Chip("xist", part, report.cols, report.rows)
    if constids_path:
        chip.strs.read_constids(str(constids_path))
    chip.set_speed_grades(["8"])
    luts = luts or {}

    chip.create_tile_type("EMPTY")
    kinds: dict[str, str] = {}
    for row in report.tiles:
        for tok in row:
            if not tok or tok == ".":
                continue
            if tok not in kinds:
                k = _tile_kind(tok)
                kinds[tok] = k
                if k == "PLC":
                    _add_plc_tile_type(chip, tok)
                elif k == "PIC":
                    _add_io_tile_type(chip, tok)
                else:
                    _add_hard_tile_type(chip, tok)

    # place tile types (every grid position needs one, including empty ones)
    for r, row in enumerate(report.tiles):
        for c, tok in enumerate(row):
            chip.set_tile_type(c, r, tok if tok and tok != "." else "EMPTY")

    # Seed a complete set of long-wire names so every tile can reach its
    # neighbours even when the XPN corpus does not cover that tile.
    _seed_long_wires(chip)

    # collect observed wires first, so pips/nodes can refer to them
    edges = list(routing_edges(xpn_paths or []))
    local_wires: dict[str, set[str]] = {}  # tile type -> local wire names
    for src, dst in edges:
        for wire in (src, dst):
            t = wire_tile(wire)
            if t is None:
                continue
            r, c = t
            if not (0 <= r < report.rows and 0 <= c < report.cols):
                continue
            tok = report.tiles[r][c]
            if not tok or tok == ".":
                continue
            local_wires.setdefault(tok, set()).add(wire_local(wire))
    for tok, wires in local_wires.items():
        tt = chip.tile_types[chip.tile_type_idx[tok]]
        for w in sorted(wires):
            if not tt.has_wire(w):
                tt.create_wire(w)

    # same-tile edges become pips; cross-tile edges become nodes
    added_pips: dict[tuple[str, str, str], bool] = {}
    used_in_node: set[tuple[int, int, str]] = set()
    for src, dst in edges:
        ts, td = wire_tile(src), wire_tile(dst)
        if ts is None or td is None:
            continue
        (rs, cs), (rd, cd) = ts, td
        if not (0 <= rs < report.rows and 0 <= cs < report.cols and
                0 <= rd < report.rows and 0 <= cd < report.cols):
            continue
        tok_s = report.tiles[rs][cs]
        tok_d = report.tiles[rd][cd]
        if not tok_s or tok_s == "." or not tok_d or tok_d == ".":
            continue
        ls, ld = wire_local(src), wire_local(dst)
        if (rs, cs) == (rd, cd):
            key = (tok_s, ls, ld)
            if key in added_pips:
                continue
            tt = chip.tile_types[chip.tile_type_idx[tok_s]]
            tt.create_pip(ls, ld)
            # The XPN edge direction is not fully documented; add the reverse
            # edge as well for bring-up so the router can use the observed
            # connection.  The bitstream fuser will need the real direction.
            tt.create_pip(ld, ls)
            added_pips[key] = True
        else:
            a = (rs, cs, ls)
            b = (rd, cd, ld)
            if a in used_in_node or b in used_in_node:
                continue
            chip.add_node([NodeWire(cs, rs, ls), NodeWire(cd, rd, ld)])
            used_in_node.update((a, b))

    # Long wires: the vendor names encode length/direction/track/segment, e.g.
    # V06S0300 at R20 connects to V06S0302 at R22, ... V06S0306 at R26.
    # The XPN only lists the endpoints, so rebuild the chains as nodes.
    _connect_long_wires(chip, report)
    _add_cib(chip)

    # package pins -> PIO bels via the vendor site string (PL12C, PT13B, ...)
    if pkg_pins:
        pkg = chip.create_package(_package_name(part))
        for pin in pkg_pins:
            loc = pad_location(pin.get("tile", ""))
            if loc is None:
                continue
            row, col, slot = loc
            if not (0 <= row < report.rows and 0 <= col < report.cols):
                continue
            tok = report.tiles[row][col]
            if not tok or tok == ".":
                continue
            try:
                bank = int(pin.get("bank", "0") or 0)
            except ValueError:
                bank = 0
            pkg.create_pad(pin.get("io", ""), f"X{col}Y{row}",
                           f"PIO_{slot}", pin.get("desc", "").strip(), bank)

    return XistChip(chip, part, family_for(part), luts, columns)


def _seed_long_wires(chip: Chip) -> None:
    """Create a standard long-wire set in every non-empty tile type."""
    dirs = {"V": "NS", "H": "EW"}
    for tt in chip.tile_types:
        name = tt.strs.strs
        # skip the EMPTY type (no wires at all)
        if tt.type_name.index not in range(0, 0) and len(tt.wires) == 0:
            continue
        for kind, ds in dirs.items():
            for d in ds:
                for ln in (2, 6):
                    for track in range(4):
                        for seg in range(0, ln + 1):
                            w = f"{kind}{ln:02d}{d}{track:02d}{seg:02d}"
                            if not tt.has_wire(w):
                                tt.create_wire(w)


def _add_cib(chip: Chip) -> None:
    """Add a per-tile crossbar between bel pins, local wires and long wires.

    This is a bring-up routing model: every source can reach every sink in the
    tile through one of eight CIB bus wires.  It is intentionally generic and
    does not yet encode the vendor fuse muxes.
    """
    src_re = re.compile(r"^(?:F\d+|Q\d+|SUM\d+|COUT\d+|JDI[A-D]|JPADDI[A-D]_PIO|"
                        r"[VH]\d{2}[NSEW]\d{2}\d{2}|.*_OUT)$")
    snk_re = re.compile(r"^(?:[A-D]\d|AI\d+_\d+|DI\d+|CIN\d+|CLK|LSR|PADDO[A-D]_PIO|JPADDO[A-D]|"
                        r"[VH]\d{2}[NSEW]\d{2}\d{2}|.*_IN)$")
    for tt in chip.tile_types:
        idx = {v: k for k, v in tt.strs.strs.items()}
        names = [idx[w.name.index] for w in tt.wires]
        bus = [f"CIB{i}" for i in range(32)]
        for b in bus:
            if not tt.has_wire(b):
                tt.create_wire(b)
        seen = set()
        def pip(a, b):
            if (a, b) in seen:
                return
            seen.add((a, b))
            tt.create_pip(a, b)
        for n in names:
            if src_re.match(n):
                for b in bus:
                    pip(n, b)
            if snk_re.match(n):
                for b in bus:
                    pip(b, n)


def _package_name(part: str) -> str:
    # SL2-12E-8F256C -> F256
    tail = part.rsplit("-", 1)[-1]
    tail = tail.lstrip("0123456789")
    return tail[:-1] if tail and tail[-1] in "CI" else tail


def pad_location(site: str, rows: int = 40, cols: int = 51) -> tuple[int, int, str] | None:
    """Decode a vendor pad site such as PL12C / PT13B / PR5A / PB7D.

    Returns (row, col, slot) for the 51x40 SL2-12E grid.
    """
    m = re.match(r"^P([LRTB])(\d+)([A-D])$", site.strip())
    if not m:
        return None
    side, num, slot = m.group(1), int(m.group(2)), m.group(3)
    if side == "L":
        return num, 0, slot
    if side == "R":
        return num, cols - 1, slot
    if side == "T":
        return 0, num, slot
    return rows - 1, num, slot


_LONG_RE = re.compile(r"^([VH])(\d{2})([NSEW])(\d{2})(\d{2})$")
_DIR_STEP = {"N": (-1, 0), "S": (1, 0), "E": (0, 1), "W": (0, -1)}


def _connect_long_wires(chip: Chip, report: DeviceReport) -> None:
    """Union long-wire segments (V06S0300 -> V06S0302 -> ...) into nodes.

    XPN only lists the endpoints of a long wire.  The vendor name encodes the
    length, direction, track and segment offset, so the full chain can be
    reconstructed and the intermediate wires created on demand.
    """
    parent: dict[tuple[int, int, str], tuple[int, int, str]] = {}

    def find(k):
        parent.setdefault(k, k)
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for r, row in enumerate(report.tiles):
        for c, tok in enumerate(row):
            if not tok or tok == ".":
                continue
            tt = chip.tile_types[chip.tile_type_idx[tok]]
            idx2str = {v: k for k, v in tt.strs.strs.items()}
            for wd in list(tt.wires):
                name = idx2str.get(wd.name.index)
                if not name:
                    continue
                m = _LONG_RE.match(name)
                if not m:
                    continue
                kind, ln, d, track, seg = m.groups()
                ln, seg = int(ln), int(seg)
                dr, dc = _DIR_STEP[d]
                for s in range(0, ln + 1):
                    step = s - seg
                    tr, tc = r + dr * step, c + dc * step
                    if not (0 <= tr < report.rows and 0 <= tc < report.cols):
                        continue
                    ntok = report.tiles[tr][tc]
                    if not ntok or ntok == ".":
                        continue
                    ntt = chip.tile_types[chip.tile_type_idx[ntok]]
                    nname = f"{kind}{ln:02d}{d}{track}{s:02d}"
                    if not ntt.has_wire(nname):
                        ntt.create_wire(nname)
                    union((r, c, name), (tr, tc, nname))

    comps: dict[tuple, list[tuple[int, int, str]]] = {}
    for k in list(parent):
        comps.setdefault(find(k), []).append(k)
    for comp in comps.values():
        if len(comp) < 2:
            continue
        try:
            chip.add_node([NodeWire(c, r, w) for r, c, w in comp])
        except AssertionError:
            # a wire already belongs to an XPN-derived node; leave it alone
            pass
