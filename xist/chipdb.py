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
    for s in range(4):
        # wires for one slice
        for w in ("A", "B", "C", "D", "F0", "F1", "Q0", "Q1",
                  "CLK", "LSR", "DI0", "DI1"):
            tt.create_wire(f"S{s}_{w}")
        p = f"S{s}_"
        l0 = tt.create_bel(f"LUT{2*s}", "LUT4", 4 * s)
        l1 = tt.create_bel(f"LUT{2*s+1}", "LUT4", 4 * s + 1)
        d0 = tt.create_bel(f"DFF{2*s}", "DFF", 4 * s + 2)
        d1 = tt.create_bel(f"DFF{2*s+1}", "DFF", 4 * s + 3)
        for lut, inp, out in ((l0, 0, 0), (l1, 1, 1)):
            for pin, w in zip("ABCD", "ABCD"):
                tt.add_bel_pin(lut, pin, p + w, PinType.INPUT)
            tt.add_bel_pin(lut, "Z", p + f"F{out}", PinType.OUTPUT)
        for dff, idx in ((d0, 0), (d1, 1)):
            tt.add_bel_pin(dff, "D", p + f"DI{idx}", PinType.INPUT)
            tt.add_bel_pin(dff, "CK", p + "CLK", PinType.INPUT)
            tt.add_bel_pin(dff, "Q", p + f"Q{idx}", PinType.OUTPUT)
        # LUT output can feed the slice DFF and the fabric output
        tt.create_pip(p + "F0", p + "DI0")
        tt.create_pip(p + "F1", p + "DI1")
        g = tt.create_group(f"SLICE{s}", "SLICE")
        for bel in (l0, l1, d0, d1):
            tt.add_bel_to_group(bel, f"SLICE{s}")


def _add_io_tile_type(chip: Chip, name: str, n_io: int = 4) -> None:
    tt = chip.create_tile_type(name)
    for s in range(n_io):
        tt.create_wire(f"IO{s}_PAD")
        tt.create_wire(f"IO{s}_I")
        tt.create_wire(f"IO{s}_O")
        tt.create_wire(f"IO{s}_OE")
        b = tt.create_bel(f"PIO_{SLICE_LETTERS[s]}", "PIO", s)
        tt.add_bel_pin(b, "PAD", f"IO{s}_PAD", PinType.INOUT)
        tt.add_bel_pin(b, "I", f"IO{s}_I", PinType.OUTPUT)
        tt.add_bel_pin(b, "O", f"IO{s}_O", PinType.INPUT)
        tt.add_bel_pin(b, "OE", f"IO{s}_OE", PinType.INPUT)


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
               columns: dict[int, int | None] | None = None) -> XistChip:
    chip = Chip("xist", part, report.cols, report.rows)
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
            added_pips[key] = True
        else:
            a = (rs, cs, ls)
            b = (rd, cd, ld)
            if a in used_in_node or b in used_in_node:
                continue
            chip.add_node([NodeWire(cs, rs, ls), NodeWire(cd, rd, ld)])
            used_in_node.update((a, b))

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
