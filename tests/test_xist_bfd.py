"""Tests for the vendor BFD parser and the Ram -> bitstream calibration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from xist.bfd import parse
from xist.bfdmap import Calibration, calibration_from_columns, load_calibration
from xist.bitstream import Bitstream
from xist.program import program_routing, routing_equations, used_arcs

TESTDATA = Path(__file__).resolve().parent.parent / "xist" / "testdata"

BFD = """\
Tile "TINY" Rows=2 Columns=3 Rams=6 Nodes=4
{
   Sites
   {
      SLICE_A Row=1,
      SLICE_B Row=0
   }

   Nodes
   {
      A,
      B,
      C = A,
      D
   }

   Ram Row=0
   {
      F0, F1, F2
   }
   Ram Row=1
   {
      F3, F4, F5
   }

   F0 = (arcval(A,B) ||
      arcval(C,B));
   F1 = arcval(B,C);
   F2 = FALSE;
   F3 = (config(SLICE_A,MODE,"X") || arcval(A,D));
   F4 = property(SLICE_B,REG0.SR,"X");
   F5 = arcval(D,A);
}
Tile "OTHER" Rows=1 Columns=1 Rams=1 Nodes=1
{
   Nodes
   {
      Z
   }
   Ram Row=0
   {
      G0
   }
   G0 = FALSE;
}
"""


def test_bfd_parse(tmp_path):
    p = tmp_path / "bfd.txt"
    p.write_text(BFD)
    tiles = parse(p)
    assert set(tiles) == {"TINY", "OTHER"}
    t = tiles["TINY"]
    assert (t.rows, t.cols) == (2, 3)
    assert t.ram == {0: ["F0", "F1", "F2"], 1: ["F3", "F4", "F5"]}
    assert t.sites == {"SLICE_A": 1, "SLICE_B": 0}
    assert len(t.nodes) == 4          # alias line inside Nodes must not leak
    assert len(t.equations) == 6
    assert t.fuse_positions("F4") == [(1, 1)]


def test_bfd_arcs(tmp_path):
    p = tmp_path / "bfd.txt"
    p.write_text(BFD)
    t = parse(p)["TINY"]
    assert t.arc_set() == {("A", "B"), ("C", "B"), ("B", "C"),
                           ("A", "D"), ("D", "A")}
    af = t.arcs()
    # F0 implements two arcs at the same bit
    assert {f["fuse"] for f in af[("A", "B")]} == {"F0"}
    assert af[("A", "B")][0]["row"] == 0 and af[("A", "B")][0]["col"] == 0
    # F3 mixes config with an arc: excluded from routing arcs()
    assert ("A", "D") not in af
    assert ("D", "A") in af


def test_calibration_formula():
    cal = Calibration({5: 100})
    assert cal.row(3, 2) == 29 * 2 + 2
    assert cal.col(5, 7) == 93
    assert cal.bit(3, 5, 2, 7) == (60, 93)
    cal2 = calibration_from_columns({"5": 56}, {5: 0})
    assert cal2.column_base[5] == 100
    cal3 = calibration_from_columns({"5": 56}, {5: -2})
    assert cal3.column_base[5] == 98


def test_real_plc_table():
    d = json.loads((TESTDATA / "SL2-12E_bfd_PLC.json").read_text())
    assert d["arcs"] == 2500
    assert len(d["fuses"]) == 1392
    assert d["routing_equations"] == 1128
    assert "V02N0002->D6" in d["arc_fuses"]
    assert d["fuses"]["XROUTING/XC01D67/MC1_XSB_0"] == [[8, 16]]


def test_calibration_predicts_fuzz_candidate():
    """The calibrated map must reproduce a measured pip fuse position."""
    d = json.loads((TESTDATA / "SL2-12E_bfd_PLC.json").read_text())
    cal = load_calibration()
    sample = json.loads((TESTDATA / "SL2-12E_pip_fuses_sample.json").read_text())
    cand = next(c for c in sample["matches"]
               if tuple(c["pip"]) == ("R20C19_D6", "R20C19_V02N0002"))
    preds = {cal.bit(20, 19, rr, rc)
             for fname in d["arc_fuses"]["V02N0002->D6"]
             for rr, rc in d["fuses"][fname]}
    assert tuple(cand["pos"]) in preds
    assert (559, 1484) in preds


def test_profiles_complete():
    d = json.loads((TESTDATA / "SL2-12E_bfd_profiles.json").read_text())
    assert set(d["profiles"]) == {"PLC", "CIB_CFG", "CIB_DSP", "CIB_EBR"}
    cov = d["coverage"]
    assert cov["arcs_union"] == 2668
    assert cov["long_wire_arcs_union"] == 1976
    assert len(cov["tile_types"]) == 22


def _make_bs(rows=40, cols=120):
    return Bitstream(
        header_lines=["XIST ASCII Bitstream", "Version:          1.0",
                      f"Rows:             {rows}", f"Cols:             {cols}"],
        header={"Rows": str(rows), "Cols": str(cols)},
        pre_block=["1010"],
        rows=["0" * cols for _ in range(rows)],
        footer_block=["1111"],
        extra16="0" * 16,
    )


def test_program_routing(tmp_path):
    p = tmp_path / "bfd.txt"
    p.write_text(BFD)
    tile = parse(p)["TINY"]
    cal = Calibration({5: 100})
    assert set(routing_equations(tile)) == {"F0", "F1", "F5"}

    xpn = tmp_path / "x.xpn"
    xpn.write_text(
        'signal "x"\n{\n   (a, p)\n   route\n   {\n'
        "      R2C5_A.R2C5_B;\n   }\n}\n")
    assert used_arcs(xpn, 2, 5) == {("A", "B")}

    bs = _make_bs()
    n = program_routing(bs, xpn, tile, cal, 2, 5)
    assert n == 3                     # F0, F1, F5 all have one position
    row = 29 * (2 - 1)
    assert bs.rows[row][100] == "1"   # F0 (A,B) used
    assert bs.rows[row][99] == "0"    # F1 (B,C) unused
    assert bs.rows[row + 1][98] == "0"  # F5 (D,A) unused
