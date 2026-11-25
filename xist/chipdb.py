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
    # tile type -> {"src->dst": [{"row", "col", "fuse"}, ...]}
    routing_fuses: dict[str, dict[str, list[dict]]] = field(default_factory=dict)
    bitcol_base: dict[int, int] | None = None

    ROW_RULE = "bit_row = 29 * (tile_row - 1) + ram_row"
    COL_RULE = "bit_col = bitcol_base[tile_col] - ram_col"

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
            "routing_fuses": self.routing_fuses or None,
            "row_rule": self.ROW_RULE,
            "col_rule": self.COL_RULE,
            "bitcol_base": ({str(k): v for k, v in self.bitcol_base.items()}
                            if self.bitcol_base else None),
            # PIO configuration is not a routing arc: the writer evaluates the
            # stored property()/config() equations against the XPN comp program.
            "pio_config": {
                "PIC": {"profile": "PIC_L5", "col_base": 2375},
                "PIC_right": {"profile": "PIC_R5", "col_base": 60},
                "file": "SL2-12E_bfd_pio.json",
                "site_rule": "XPN site P<side><row><slot> -> BFD site PIO_<slot>",
            },
        }
        Path(path).write_text(json.dumps(meta, indent=2) + "\n")


def _add_plc_tile_type(chip: Chip, name: str) -> None:
    tt = chip.create_tile_type(name)
    # one clock/reset wire per slice; the BFD routing arcs drive CLK0..CLK3 /
    # LSR0..LSR3, so the DFF clock pin must live on the slice's own wire.
    for s in range(4):
        for w in (f"CLK{s}", f"LSR{s}"):
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
            tt.add_bel_pin(dff, "CLK", f"CLK{s}", PinType.INPUT)
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



TESTDATA = Path(__file__).resolve().parent / "testdata"

# Contextual tile types.  A single UDEV token can cover physically different
# CIB blocks: CTEST is the top/bottom CIB ring (row 1/38), the EBR/DSP row
# bands, and the 59-column left/right edges; EBR and D0..D8 likewise have a
# 48-column body and 59-column edge variants.
def _context_name(tok: str, r: int, c: int) -> str:
    if tok == "PLC":
        return "PLC"
    if tok == "CTEST":
        if c == 1:
            return "CTEST_L"
        if c == 49:
            return "CTEST_R"
        if r in (1, 38):
            return "CTEST_CFG"
        return "CTEST_EBR"
    if tok == "EBR":
        if c == 1:
            return "EBR_L"
        if c == 49:
            return "EBR_R"
        return "EBR_CIB"
    if len(tok) == 2 and tok[0] == "D" and tok[1].isdigit():
        if c == 1:
            return "DSP_L"
        if c == 49:
            return "DSP_R"
        return "DSP_CIB"
    return tok


def _context_kind(tok: str, r: int, c: int) -> str:
    name = _context_name(tok, r, c)
    if name == "PLC":
        return "PLC"
    if name.startswith("PIC"):
        return "PIC"
    return "HARD"


def default_bfd_profiles() -> dict[str, tuple[Path, str | None]]:
    """Context tile type -> (profile file, profile name) for the edge CIBs."""
    prof = TESTDATA / "SL2-12E_bfd_profiles.json"
    plc = TESTDATA / "SL2-12E_bfd_PLC.json"
    return {
        "PLC": (plc, None),
        "CTEST_CFG": (prof, "CIB_CFG"),
        "CTEST_EBR": (prof, "CIB_EBR"),
        "EBR_CIB": (prof, "CIB_EBR"),
        "DSP_CIB": (prof, "CIB_DSP"),
        # 59-column left / right edge blocks (columns 1 and 49)
        "CTEST_L": (prof, "CIB_EBR0_END0_NO_CLKDLY30"),
        "EBR_L": (prof, "CIB_EBR0_END0_NO_CLKDLY30"),
        "DSP_L": (prof, "CIB_EBR0_END0_NO_CLKDLY30"),
        "CTEST_R": (prof, "CIB_EBR2_END1_SP_12K"),
        "EBR_R": (prof, "CIB_EBR2_END1_SP_12K"),
        "DSP_R": (prof, "CIB_EBR2_END1_SP_12K"),
    }


def build_chip(part: str, report: DeviceReport,
               luts: dict[str, LutCalibration] | None = None,
               xpn_paths: list[str] | None = None,
               pkg_pins: list[dict] | None = None,
               columns: dict[int, int | None] | None = None,
               constids_path: str | Path | None = None,
               bfd_profiles: dict[str, str | Path] | None = None,
               replace_cib: bool = False,
               bitcol_base: dict[int, int] | None = None) -> XistChip:
    chip = Chip("xist", part, report.cols, report.rows)
    if constids_path:
        chip.strs.read_constids(str(constids_path))
    chip.set_speed_grades(["8"])
    luts = luts or {}

    chip.create_tile_type("EMPTY")
    kinds: dict[str, str] = {}
    ctx: dict[tuple[int, int], str] = {}
    for r, row in enumerate(report.tiles):
        for c, tok in enumerate(row):
            if not tok or tok == ".":
                ctx[(r, c)] = "EMPTY"
                continue
            name = _context_name(tok, r, c)
            ctx[(r, c)] = name
            if name in kinds:
                continue
            k = _context_kind(tok, r, c)
            kinds[name] = k
            if k == "PLC":
                _add_plc_tile_type(chip, name)
            elif k == "PIC":
                _add_io_tile_type(chip, name)
            else:
                _add_hard_tile_type(chip, name)

    # place tile types (every grid position needs one, including empty ones)
    for (r, c), name in ctx.items():
        chip.set_tile_type(c, r, name)

    # Seed a complete set of long-wire names so every tile can reach its
    # neighbours even when the XPN corpus does not cover that tile.
    _seed_long_wires(chip)

    # Vendor BFD profiles: add the complete routing muxes for a tile type.
    routing_fuses: dict[str, dict[str, list[dict]]] = {}
    bfd_pips: set[tuple[str, str, str]] = set()
    if bfd_profiles:
        from .bfd_chipdb import install_profile, load_profile
        for token, spec in bfd_profiles.items():
            if token not in chip.tile_type_idx:
                continue
            path, name = spec if isinstance(spec, (tuple, list)) else (spec, None)
            arcs = install_profile(chip, token, load_profile(path, name))
            routing_fuses[token] = arcs
            bfd_pips.update((token, *arc.split("->", 1)) for arc in arcs)

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
            tok = ctx.get((r, c))
            if not tok or tok == "EMPTY":
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
        tok_s = ctx.get((rs, cs))
        tok_d = ctx.get((rd, cd))
        if not tok_s or tok_s == "EMPTY" or not tok_d or tok_d == "EMPTY":
            continue
        ls, ld = wire_local(src), wire_local(dst)
        if (rs, cs) == (rd, cd):
            key = (tok_s, ls, ld)
            if key in added_pips or key in bfd_pips:
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
    _connect_long_wires(chip, report, ctx)
    _add_cib(chip, skip=set(bfd_profiles or {}) if replace_cib else set())

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
            tok = ctx.get((row, col))
            if not tok or tok == "EMPTY":
                continue
            try:
                bank = int(pin.get("bank", "0") or 0)
            except ValueError:
                bank = 0
            pkg.create_pad(pin.get("io", ""), f"X{col}Y{row}",
                           f"PIO_{slot}", pin.get("desc", "").strip(), bank)

    return XistChip(chip, part, family_for(part), luts, columns,
                    routing_fuses=routing_fuses, bitcol_base=bitcol_base)


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


def _add_cib(chip: Chip, skip: set[str] | None = None) -> None:
    """Add a per-tile crossbar between bel pins, local wires and long wires.

    This is a bring-up routing model: every source can reach every sink in the
    tile through one of eight CIB bus wires.  It is intentionally generic and
    does not yet encode the vendor fuse muxes.
    """
    src_re = re.compile(r"^(?:F\d+|Q\d+|SUM\d+|COUT\d+|JDI[A-D]|JPADDI[A-D]_PIO|"
                        r"[VH]\d{2}[NSEW]\d{2}\d{2}|.*_OUT)$")
    snk_re = re.compile(r"^(?:[A-D]\d|AI\d+_\d+|DI\d+|CIN\d+|CLK\d?|LSR\d?|PADDO[A-D]_PIO|JPADDO[A-D]|"
                        r"[VH]\d{2}[NSEW]\d{2}\d{2}|.*_IN)$")
    skip = skip or set()
    idx_all = {v: k for k, v in chip.strs.strs.items()}
    for tt in chip.tile_types:
        if idx_all.get(tt.type_name.index) in skip:
            continue
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


def _connect_long_wires(chip: Chip, report: DeviceReport,
                        ctx: dict[tuple[int, int], str]) -> None:
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

    # length-0 boundary wires: H00R in a tile meets H00L in the tile to its
    # right; V00B meets V00T in the tile below.  There are four tracks each.
    for r, row in enumerate(report.tiles):
        for c, tok in enumerate(row):
            name = ctx.get((r, c))
            if not name or name == "EMPTY":
                continue
            tt = chip.tile_types[chip.tile_type_idx[name]]
            for track in range(4):
                for a, b, dr, dc in ((f"H00R{track:02d}00", f"H00L{track:02d}00", 0, 1),
                                     (f"V00B{track:02d}00", f"V00T{track:02d}00", 1, 0)):
                    tr, tc = r + dr, c + dc
                    if not (0 <= tr < report.rows and 0 <= tc < report.cols):
                        continue
                    if not tt.has_wire(a):
                        continue
                    nname = ctx.get((tr, tc))
                    if not nname or nname == "EMPTY":
                        continue
                    ntt = chip.tile_types[chip.tile_type_idx[nname]]
                    if ntt.has_wire(b):
                        union((r, c, a), (tr, tc, b))

    for r, row in enumerate(report.tiles):
        for c, tok in enumerate(row):
            name = ctx.get((r, c))
            if not name or name == "EMPTY":
                continue
            tt = chip.tile_types[chip.tile_type_idx[name]]
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
                    nname_tok = ctx.get((tr, tc))
                    if not nname_tok or nname_tok == "EMPTY":
                        continue
                    ntt = chip.tile_types[chip.tile_type_idx[nname_tok]]
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
