"""Tests for LUT INIT modelling and the .bit assembler."""

from __future__ import annotations

import json
from pathlib import Path

from xist.bitstream import Bitstream, LutSlot
from xist.build import _fuse_arcs, _site_refs
from xist.lutinit import (eval_function, init_from_function, varmap_candidates,
                          write_slice_luts)
from xist.sliceconfig import parse_slice_program, slice_site

TESTDATA = Path(__file__).resolve().parent.parent / "xist" / "testdata"
PLC = TESTDATA / "SL2-12E_bfd_PLC.json"


def test_eval_function():
    assert eval_function("A*B", {"A": 1, "B": 1, "C": 0, "D": 0}) == 1
    assert eval_function("A*B", {"A": 1, "B": 0, "C": 0, "D": 0}) == 0
    assert eval_function("(A+B)", {"A": 0, "B": 1, "C": 0, "D": 0}) == 1
    assert eval_function("~A", {"A": 1, "B": 0, "C": 0, "D": 0}) == 0
    assert eval_function("(A@B)", {"A": 1, "B": 1, "C": 0, "D": 0}) == 0
    assert eval_function("(D*(A*B))", {"A": 1, "B": 1, "C": 0, "D": 0}) == 0


def test_init_natural_order():
    vm = {"A": 0, "B": 1, "C": 2, "D": 3}
    assert init_from_function("A*B", vm) == 0x8888
    assert init_from_function("(D*(A*B))", vm) == 0x8800
    assert init_from_function("A", vm) == 0xAAAA
    assert len(varmap_candidates()) == 24


def test_parse_slice_program():
    prog = ("MODE:LOGIC K1::H1=(A*D) K0::H0=(A*B) REG0:::REGSET=RESET,"
            "CLKDELAY=DEL0:SD=1 GSR:ENABLED CLKMUX:CLK:::0=0,CLK=#SIG "
            "SRMODE:ASYNC LSRONMUX:0 REGMODE:FF")
    props, cfgs, funcs = parse_slice_program(prog)
    assert funcs == {"K1": "(A*D)", "K0": "(A*B)"}
    assert props["REG0.REGSET"] == "RESET"
    assert props["REG0.CLKDELAY"] == "DEL0"
    assert ("REGMODE", "FF") in cfgs
    assert ("REG0.SD", "1") in cfgs
    assert ("CLKMUX.CLK", "SIG") in cfgs
    assert slice_site("R20C19D") == "SLICE_D"
    assert slice_site("PL2A") is None or slice_site("PL2A") == "SLICE_A"


def test_bfd_lut_init_table():
    d = json.loads(PLC.read_text())
    lut = d["lut_init"]
    assert set(lut) == {"SLICE_A", "SLICE_B", "SLICE_C", "SLICE_D"}
    for site in lut:
        assert set(lut[site]) == {"0", "1"}
        assert len(lut[site]["0"]) == 16
        assert len(lut[site]["1"]) == 16
        assert all(p is not None for p in lut[site]["0"])
    # slice D bank 0 minterm 0 is at Ram (27, 37)
    assert lut["SLICE_D"]["0"][0] == [27, 37]


def test_write_slice_luts(tmp_path):
    d = json.loads(PLC.read_text())
    # synthetic slots with the same shape as the real table
    slots = {"R1C1D": {"f0": d["lut_init"]["SLICE_D"]["0"],
                       "varmap": {"A": 0, "B": 1, "C": 2, "D": 3}}}
    rows = ["0" * 40 for _ in range(60)]
    bs = Bitstream(header_lines=["h"], header={"Rows": "60", "Cols": "40"},
                   pre_block=[], rows=rows, footer_block=[], extra16="")
    n = write_slice_luts(bs, slots, "R1C1D", {"F0": "A*B"})
    assert n == 16
    init = init_from_function("A*B", {"A": 0, "B": 1, "C": 2, "D": 3})
    for j, (r, c) in enumerate(d["lut_init"]["SLICE_D"]["0"]):
        assert bs.rows[r][c] == ("0" if (init >> j) & 1 else "1")


def test_fuse_arcs_and_site_refs():
    prof = json.loads(PLC.read_text())
    af = _fuse_arcs(prof)
    assert ("V02N0002", "D6") in af["XROUTING/XC01D67/MC1_XSB_0"]
    from xist.bfd_expr import parse_cached
    node = parse_cached('config(SLICE_A,REGMODE,"FF")')
    assert _site_refs(node) == {"SLICE_A"}
