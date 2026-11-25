"""Bitstream writer tests: routed-data -> .bit, including reset/CE slices.

The strongest check replays ``counter.xpn`` through the *routed-data* API
(``write_bitstream(pips=..., cells=...)``) onto a blank device image and
compares the result with the vendor bitstream of the same design.  ``counter``
uses DFFs with a shared CE and LSR (reset), so the slice-program synthesis and
the CE/LSR routing fuses are covered too.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from xist.bitgen import (SLICE_LETTERS, routed_cells, routed_pips,
                         slice_facts, write_bitstream)
from xist.apply import _config, apply_pio_comps
from xist.pioconfig import bits_from_equations
from xist.piogen import PIO_IN, PIO_OUT
from xist.bitstream import Bitstream
from xist.lutinit import init_from_function
from xist.bfd_expr import evaluate, parse_cached
from xist.pioconfig import env_for
from xist.sliceconfig import parse_slice_program
from xist.sliceprog import slice_program
from xist.xpn import parse_comps, parse_xpn

TESTDATA = Path(__file__).resolve().parent.parent / "xist" / "testdata"
PLCPROF = TESTDATA / "SL2-12E_bfd_PLC.json"
SLICEJSON = TESTDATA / "SL2-12E_bfd_slice.json"
VENDOR_BIT = TESTDATA / "SL2-12E_counter_vendor.bit"
EMPTY_BIT = TESTDATA / "SL2-12E_empty_vendor.bit"
COUNTER_XPN = TESTDATA / "counter.xpn"
VARMAP = {"A": 0, "B": 1, "C": 2, "D": 3}
PLC_ROWS = range(29, 1074)          # tile_row 2..38, 29 RAM rows each


def xpn_view(path: Path):
    """Build the (pips, cells) view of an XPN for write_bitstream()."""
    cells: dict = {}
    for comp in parse_comps(path).values():
        m = re.match(r"R(\d+)C(\d+)([A-D])$", comp.site or "")
        if not m:
            continue
        row, col, letter = int(m.group(1)), int(m.group(2)), m.group(3)
        props, cfgs, funcs = parse_slice_program(comp.program)
        slot = cells.setdefault((row, col), {}).setdefault(
            letter, {"luts": set(), "dffs": set(), "inits": {},
                     "reset": False, "ce": False, "sync_reset": False})
        for bank, key in ((0, "K0"), (1, "K1")):
            if key in funcs:
                slot["luts"].add(bank)
                slot["inits"][bank] = init_from_function(funcs[key], VARMAP)
        for bank in (0, 1):
            if (f"REG{bank}.REGSET" in props or
                    any(k == f"REG{bank}.SD" for k, _ in cfgs)):
                slot["dffs"].add(bank)
        slot["reset"] = slot["reset"] or any(k == "LSRMUX.LSR" for k, _ in cfgs)
        slot["ce"] = slot["ce"] or any(k == "CEMUX.CE" for k, _ in cfgs)
        slot["sync_reset"] = ("SRMODE", "LSR_OVER_CE") in cfgs

    pips: dict = {}
    for sig in parse_xpn(path).values():
        for a, b in sig.route:
            ma, mb = (re.match(r"R(\d+)C(\d+)_", x) for x in (a, b))
            pair = (b.split("_", 1)[1], a.split("_", 1)[1])
            for m in (ma, mb):
                if m:
                    pips.setdefault((int(m.group(1)), int(m.group(2)),
                                     ), set()).add(pair)
    return pips, cells


def _patch_sync_reset(monkeypatch):
    orig = slice_facts

    def facts(slot):
        f = orig(slot)
        if slot.get("sync_reset"):
            f.reset_is_async = False
        return f

    monkeypatch.setattr("xist.bitgen.slice_facts", facts)


@pytest.fixture(scope="module")
def counter_replay_bit(tmp_path_factory):
    """Replay counter.xpn once: its slices use a synchronous reset + CE."""
    if not (VENDOR_BIT.exists() and EMPTY_BIT.exists()):
        pytest.skip("vendor bitstreams not available")
    mp = pytest.MonkeyPatch()
    _patch_sync_reset(mp)
    try:
        pips, cells = xpn_view(COUNTER_XPN)
        out = tmp_path_factory.mktemp("replay") / "replay.bit"
        write_bitstream(EMPTY_BIT, out, pips=pips, cells=cells, verbose=False)
    finally:
        mp.undo()
    return out





def test_routed_pips_and_cells_parse():
    routed = {"modules": {"m": {
        "attributes": {"top": "00000000000000000000000000000001"},
        "netnames": {"n": {"attributes": {"ROUTING":
            "X2Y2/F0;;1;X2Y2/DI0;X2Y2/DI0/F0;1;X2Y2/CE0;X2Y2/CE0/F0;1"}}},
        "cells": {
            "lut": {"type": "LUT4", "attributes": {"NEXTPNR_BEL": "X2Y2/LUT0"},
                    "parameters": {"INIT": "1000000000000000"},
                    "connections": {"F": [7]}},
            "dff": {"type": "DFF", "attributes": {"NEXTPNR_BEL": "X2Y2/DFF1"},
                    "parameters": {"RESET": "1", "CE": "1"},
                    "connections": {"D": [7], "CLK": [8], "R": [9], "CE": [10]}},
        }}}}
    pips = routed_pips(routed)
    assert pips == {(2, 2): {("F0", "DI0"), ("F0", "CE0")}}
    cells = routed_cells(routed)
    slot = cells[(2, 2)]["A"]
    assert slot["luts"] == {0} and slot["inits"][0] == 0x8000
    assert slot["dffs"] == {1} and slot["reset"] and slot["ce"]
    facts = slice_facts(slot)
    assert facts.reset and facts.ce and facts.sd_from_lut
    program = slice_program(facts)
    assert "LSRMUX:LSR" in program and "CEMUX:1" in program


def test_counter_replay_writes_exact_plc_bits(counter_replay_bit):
    """Replay a DFF+CE+reset design onto a blank image; PLC bits must match."""
    out = counter_replay_bit

    from xist.bfdmap import load_calibration
    cal = load_calibration()
    # interior PLC tile columns (2..48); columns 1 and 49 carry PIO
    # configuration (FIBMEM etc.), which this writer does not emit yet
    interior = {cal.column_base[tc] - rc for tc in range(2, 49)
                if tc in cal.column_base for rc in range(48)}
    vendor = Bitstream.read(str(VENDOR_BIT))
    ours = Bitstream.read(str(out))
    diff = [(r, c) for r, (a, b) in enumerate(zip(vendor.rows, ours.rows))
            for c, (x, y) in enumerate(zip(a, b))
            if x != y and r in PLC_ROWS and c in interior]
    assert diff == [], f"{len(diff)} residual PLC bits, first {diff[:8]}"


NEXTPNR = Path.home() / "Downloads/nextpnr/build/nextpnr-himbaechel"


@pytest.mark.skipif(shutil.which("yosys") is None or not NEXTPNR.exists(),
                    reason="needs yosys and a built nextpnr-himbaechel")
def test_own_routed_ce_reset_design(tmp_path):
    """A design with async reset + CE: place/route, then write the .bit."""
    src = tmp_path / "ctr4.v"
    src.write_text(
        "module top(input clk, input rst, input en, output reg [3:0] q,"
        " output led);\n"
        "  always @(posedge clk or posedge rst) if (rst) q <= 4'd0;\n"
        "  else if (en) q <= q + 1'b1;\n"
        "  assign led = &q;\n"
        "endmodule\n")
    from xist.demo_any import main as demo

    assert demo([str(src), "--bfd-replace", "--workdir", str(tmp_path / "w"),
                 "--nextpnr", str(NEXTPNR)]) == 0
    routed = json.loads((tmp_path / "w" / "routed.json").read_text())
    out = tmp_path / "ctr4.bit"
    res = write_bitstream(EMPTY_BIT, out, routed=routed, verbose=False)
    assert res["checksum"]
    assert res["pio"] >= 95      # one edge pad at least

    from xist.bfdmap import load_calibration
    cal = load_calibration()
    plc = json.loads(PLCPROF.read_text())
    sl = json.loads(SLICEJSON.read_text())["fuses"]
    pips = routed_pips(routed)
    ours = Bitstream.read(str(out))

    def bit_of(row, col, rr, rc):
        br, bc = cal.bit(row, col, rr, rc)
        return ours.rows[br][bc]

    def slice_lane(row, col, letter):
        s = SLICE_LETTERS.index(letter)
        return row, col, f"CE{s}", f"LSR{s}"

    checked = 0
    for cell in list(routed["modules"].values())[0]["cells"].values():
        if cell["type"] != "DFF":
            continue
        if "PAD_REG" in cell.get("parameters", {}):
            continue        # lives in the pad's IOLOGIC, not in a slice
        bel = cell.get("attributes", {}).get("NEXTPNR_BEL", "")
        m = re.match(r"X(\d+)Y(\d+)/DFF(\d)$", bel)
        if not m:
            continue
        col, row, bank = int(m.group(1)), int(m.group(2)), int(m.group(3))
        letter = SLICE_LETTERS[bank // 2]
        site = f"SLICE_{letter}"
        s = SLICE_LETTERS.index(letter)

        # routing into the slice-wide CE / LSR muxes
        for wire in (f"CE{s}", f"LSR{s}"):
            for src, dst in pips.get((row, col), set()):
                if dst != wire:
                    continue
                for name in plc["arc_fuses"].get(f"{src}->{dst}", []):
                    for rr, rc in plc["fuses"][name]:
                        assert bit_of(row, col, rr, rc) == "1"
                    checked += 1

        # slice configuration: REGSET is written only for the used bank (the
        # vendor leaves the other bank's fuse at 0), LSR mux on, async mode
        bank = int(bel.split("DFF")[1])
        for name, info in sl.items():
            if site not in info["expr"]:
                continue
            last = name.split("/")[-1]
            if last == f"MC1_SR_{bank % 2}":
                for rr, rc in info["positions"]:
                    assert bit_of(row, col, rr, rc) == "1"
            elif last == "MC1_NOLSR":
                for rr, rc in info["positions"]:
                    assert bit_of(row, col, rr, rc) == "0"
            elif last.startswith("MC1_ASYNCMODE"):
                for rr, rc in info["positions"]:
                    assert bit_of(row, col, rr, rc) == "1"
    assert checked > 0


def test_pio_defaults_match_vendor_pads():
    """Synthesised pad programs reproduce vendor config for real user pads."""
    from xist.xpn import Comp
    from xist.bfdmap import load_calibration

    data, eqs = _config("")
    cal = load_calibration()
    comps = list(parse_comps(COUNTER_XPN).values())
    vendor = Bitstream.read(str(VENDOR_BIT))
    pads = {"PL12C": "L", "PB16A": "B", "PT19A": "T"}
    seen = 0
    for comp in comps:
        if comp.site not in pads:
            continue
        side = pads[comp.site]
        pos = int(comp.site[2:-1])
        synth = Comp(comp.name, comp.site,
                     PIO_IN if "PADDI:PADDI" in comp.program else PIO_OUT)
        if side in "LR":
            base, rbase, tile_row = (2375 if side == "L" else 60), None, pos
        else:
            base = cal.column_base[pos] + data["column_delta"].get(
                side, {}).get(str(pos), 0)
            rbase, tile_row = data["row_base"][side], pos
        bits = bits_from_equations(eqs[side], [synth], tile_row, base, rbase)
        for br, bc, val in bits:
            assert (vendor.rows[br][bc] == "1") == val, (comp.site, br, bc)
        seen += 1
    assert seen == 3


def _edge_config_mismatches(xpn: Path, vendor: Path, side: str, pos: int,
                            profile: str):
    from xist.apply import pad_group
    from xist.bfdmap import load_calibration

    cal = load_calibration()
    profiles = json.loads((TESTDATA / "SL2-12E_bfd_profiles.json").read_text())
    prof = profiles["profiles"][profile]
    pio = json.loads((TESTDATA / "SL2-12E_bfd_pio.json").read_text())
    comps = [c for c in parse_comps(xpn).values()
             if pad_group(c.site) == (side, pos)]
    env = env_for(comps)
    v = Bitstream.read(str(vendor))
    if side in "LR":
        base, rbase, tr = (2375 if side == "L" else 60), None, pos
    else:
        base = cal.column_base[pos] + pio["column_delta"].get(
            side, {}).get(str(pos), 0)
        rbase, tr = pio["row_base"][side], pos
    diff = []
    for name, expr in prof["fuse_exprs"].items():
        if "arcval" in expr:
            continue
        if "config(" not in expr and "property(" not in expr:
            continue
        if not any(f"IOLOGIC_{i}" in expr for i in range(4)) and \
                "PIO_" not in expr:
            continue
        val = evaluate(parse_cached(expr), env)
        for rr, rc in prof["fuses"].get(name, []):
            br = (rbase if rbase is not None else 29 * (tr - 1)) + rr
            bc = base - rc
            if bc < 0:
                continue
            if (v.rows[br][bc] == "1") != val:
                diff.append((name.split("/")[-1], br, bc, val))
    return diff


def test_iologic_oreg_config_matches_vendor():
    """Counter's output-register IOLOGIC program reproduces all pad config bits."""
    diff = _edge_config_mismatches(COUNTER_XPN, VENDOR_BIT, "B", 19, "PIC_B5")
    assert diff == []


def test_iologic_ireg_config_matches_vendor():
    """An input register at the pad (IOL_L2A) reproduces the vendor config bits."""
    xpn = TESTDATA / "SL2-12E_ireg.xpn"
    bit = TESTDATA / "SL2-12E_ireg_vendor.bit"
    if not bit.exists():
        pytest.skip("vendor bitstream not available")
    diff = _edge_config_mismatches(xpn, bit, "L", 2, "PIC_L5")
    assert diff == []


@pytest.mark.skipif(shutil.which("yosys") is None or not NEXTPNR.exists(),
                    reason="needs yosys and a built nextpnr-himbaechel")
def test_output_register_packs_into_iologic(tmp_path):
    """A register that only drives a pad moves into the pad's IOLOGIC block."""
    src = tmp_path / "reg_out.v"
    src.write_text(
        "module top(input clk, input a, input b, output reg q);\n"
        "  always @(posedge clk) q <= a & b;\n"
        "endmodule\n")
    from xist.demo_any import main as demo

    assert demo([str(src), "--bfd-replace", "--workdir", str(tmp_path / "w"),
                 "--nextpnr", str(NEXTPNR)]) == 0
    routed = json.loads((tmp_path / "w" / "routed.json").read_text())
    cells = list(routed["modules"].values())[0]["cells"]

    pad_reg = [c for c in cells.values()
               if c["type"] == "DFF" and "PAD_REG" in c.get("parameters", {})]
    assert len(pad_reg) == 1, "output register was not packed into IOLOGIC"
    bel = pad_reg[0]["attributes"]["NEXTPNR_BEL"]
    assert "/IOLO" in bel, bel

    out = tmp_path / "reg_out.bit"
    res = write_bitstream(EMPTY_BIT, out, routed=routed, verbose=False)
    assert res["pio"] > 0

    # the pad now takes its data from IOLDO, and the IOLOGIC output register is
    # enabled in the edge profile fuses
    from xist.bfd_expr import evaluate, parse_cached
    from xist.pioconfig import env_for
    from xist.piogen import iologic_comps, pio_comps

    side = "L" if "Y2" in bel and "X0" in bel else None
    profiles = json.loads((TESTDATA / "SL2-12E_bfd_profiles.json").read_text())
    prof = profiles["profiles"]["PIC_L5"]
    comps = [c for c in pio_comps(routed) + iologic_comps(routed)
             if c.site.startswith("PL") or c.site.startswith("IOL_L")]
    env = env_for(comps)
    bs = Bitstream.read(str(out))
    from xist.bfdmap import load_calibration
    cal = load_calibration()
    row, col = int(bel.split("Y")[1].split("/")[0]), int(bel[1:].split("Y")[0])
    base = 2375
    checked = 0
    for name in ("MC1_OUTFF_0", "MC1_OUTCE_EN_0", "MC1_OUTCLK_EN_0"):
        fuse = next(k for k in prof["fuse_exprs"] if k.endswith("/" + name))
        assert evaluate(parse_cached(prof["fuse_exprs"][fuse]), env) is True
        for rr, rc in prof["fuses"][fuse]:
            br = 29 * (row - 1) + rr
            bc = base - rc
            if bc < 0:
                continue
            assert bs.rows[br][bc] == "1", (name, br, bc)
            checked += 1
    assert checked > 0


@pytest.mark.parametrize("name,profile,prog", [
    ("SL2-12E_ddr_oddr", "PIC_L5", "IOLOGIC_ODDR"),
    ("SL2-12E_ddr_iddr", "PIC_L5", "IOLOGIC_IDDR"),
])
def test_ddr_pad_config_matches_vendor(name, profile, prog):
    """ODDR / IDDR at a pad: every edge config fuse matches the vendor .bit."""
    from xist import piogen
    from xist.apply import _config, profile_config_equations, pad_group
    from xist.bfdmap import load_calibration
    from xist.pioconfig import bits_from_equations
    from xist.xpn import Comp

    xpn = TESTDATA / f"{name}.xpn"
    bit = TESTDATA / f"{name}_vendor.bit"
    if not bit.exists():
        pytest.skip("vendor bitstream not available")
    data, eqs = _config("")
    profiles = json.loads((TESTDATA / "SL2-12E_bfd_profiles.json").read_text())
    cal = load_calibration()
    pos = 2                                   # the vendors placed both at PL2A
    comps = [Comp(c.name, c.site, getattr(piogen, prog))
             if c.site.startswith("IOL_") else c
             for c in parse_comps(xpn).values()
             if pad_group(c.site) == ("L", pos)]
    assert any(c.site.startswith("IOL_") for c in comps)
    bits = bits_from_equations(
        list(eqs["L"]) + profile_config_equations(profiles["profiles"][profile]),
        comps, pos, 2375, None)
    vendor = Bitstream.read(str(bit))
    diff = [(br, bc) for br, bc, val in bits
            if bc >= 0 and (vendor.rows[br][bc] == "1") != val]
    assert diff == [], f"{len(diff)} mismatching PIO/IOLOGIC bits {diff[:6]}"


def test_counter_control_fuses_set(counter_replay_bit):
    """Reset/CE produce the expected slice and routing fuses."""
    ours = Bitstream.read(str(counter_replay_bit))

    from xist.bfdmap import load_calibration
    plc = json.loads(PLCPROF.read_text())
    sl = json.loads(SLICEJSON.read_text())["fuses"]
    cal = load_calibration()
    row, col = 20, 19

    def slice_bits(needle):
        vals = []
        for name, info in sl.items():
            if needle in name and "SLICE_C" in info["expr"]:
                for rr, rc in info["positions"]:
                    br, bc = cal.bit(row, col, rr, rc)
                    vals.append(ours.rows[br][bc])
        return "".join(vals)

    assert slice_bits("MC1_SR_0") == "1"      # REGSET=RESET
    assert slice_bits("MC1_NOLSR") == "0"     # LSR mux enabled
    assert slice_bits("MC1_ASYNCMODE") == "0"  # SRMODE=LSR_OVER_CE

    def arc_bits(arc):
        vals = []
        for name in plc["arc_fuses"].get(arc, []):
            for rr, rc in plc["fuses"][name]:
                br, bc = cal.bit(row, col, rr, rc)
                vals.append(ours.rows[br][bc])
        return "".join(vals)

    # counter routes its reset/CE net to the slice LSR mux
    assert set(arc_bits("H02E0701->LSR1")) == {"1"}
