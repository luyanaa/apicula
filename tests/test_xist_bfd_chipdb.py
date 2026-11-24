"""Tests for installing vendor BFD routing profiles into the chipdb."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from xist.chipdb import build_chip
from xist.device import parse_pkg_report, parse_udev_report
from xist.lutmap import load

TESTDATA = Path(__file__).resolve().parent.parent / "xist" / "testdata"


@pytest.fixture(scope="module")
def chip_bfd():
    rep = parse_udev_report((TESTDATA / "SL2-12E-8F256C_udev.txt").read_text())
    pkg = parse_pkg_report((TESTDATA / "SL2-12E-8F256C_pkg.txt").read_text())
    bitcol = {int(k): v for k, v in json.loads(
        (TESTDATA / "SL2-12E_bitcol_base.json").read_text())["column_base"].items()}
    chip = build_chip(
        "SL2-12E-8F256C", rep,
        luts={"A": load(TESTDATA / "SL2-12E_lut_A.json")},
        xpn_paths=[], pkg_pins=pkg,
        columns=json.loads((TESTDATA / "SL2-12E_columns.json").read_text()),
        constids_path=TESTDATA.parent / "nextpnr/uarch/xist/constids.inc",
        bfd_profiles={"PLC": TESTDATA / "SL2-12E_bfd_PLC.json"},
        replace_cib=True,
        bitcol_base=bitcol,
    )
    return chip


def _pip_names(chip, token):
    tt = chip.chip.tile_types[chip.chip.tile_type_idx[token]]
    idx = {v: k for k, v in tt.strs.strs.items()}
    return tt, {(idx[tt.wires[p.src_wire].name.index],
                 idx[tt.wires[p.dst_wire].name.index]) for p in tt.pips}


def test_bfd_pips_installed(chip_bfd):
    tt, pips = _pip_names(chip_bfd, "PLC")
    arcs = set(chip_bfd.routing_fuses["PLC"])
    assert len(arcs) == 2500
    missing = {a for a in arcs if tuple(a.split("->", 1)) not in pips}
    assert not missing
    assert ("V02N0002", "D6") in pips
    # slice-internal pips (F -> DI) are still present but have no routing fuse
    assert ("F0", "DI0") in pips
    assert "F0->DI0" not in arcs


def test_bfd_fuse_positions(chip_bfd):
    fuses = chip_bfd.routing_fuses["PLC"]["V02N0002->D6"]
    assert {"row": 8, "col": 16, "fuse": "XROUTING/XC01D67/MC1_XSB_0"} in fuses


def test_bfd_meta(chip_bfd, tmp_path):
    out = tmp_path / "meta.json"
    chip_bfd.write_meta(out)
    meta = json.loads(out.read_text())
    assert len(meta["routing_fuses"]["PLC"]) == 2500
    assert meta["bitcol_base"]["19"] == 1500
    assert meta["row_rule"].startswith("bit_row = 29")
    assert meta["col_rule"].startswith("bit_col = bitcol_base")
