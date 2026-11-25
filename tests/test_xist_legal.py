"""Tests for DFF semantics, constants and BFD-derived slice legality."""

from __future__ import annotations

from pathlib import Path

from xist.nextpnr_json import convert
from xist.slicelegal import (CARRY_MODES, MODE_VALUES, SliceModel, SliceUse,
                             bank_fields, bel_location_valid, load_slice_model,
                             problems)

TESTDATA = Path(__file__).resolve().parent.parent / "xist" / "testdata"


def test_slice_model_fields():
    model = load_slice_model()
    assert "MODE" in model.config_fields
    assert "REGMODE" in model.config_fields
    assert "REG0.REGSET" in model.property_fields
    assert set(MODE_VALUES) >= {"LOGIC", "RIPPLE", "CCU2"}
    b0 = bank_fields(model, 0)
    b1 = bank_fields(model, 1)
    assert "REG0.REGSET" in b0 and "REG1.REGSET" not in b0
    assert "REG1.REGSET" in b1 and "REG0.REGSET" not in b1
    assert "MODE" in b0 and "MODE" in b1       # shared slice-level fields


def test_legal_uses():
    assert problems(SliceUse(has_lut=True)) == []
    assert problems(SliceUse(has_lut=True, has_dff=True)) == []
    assert problems(SliceUse(mode="RIPPLE", has_lut=True, has_dff=True)) == []


def test_illegal_uses():
    assert problems(SliceUse(has_dff=True))        # no data path
    assert problems(SliceUse(mode="RAMW", has_lut=True, has_dff=True))
    assert problems(SliceUse(mode="RIPPLE", has_dff=True))
    assert problems(SliceUse(has_lut=True, has_dff=True,
                             needs_reset=True, needs_set=True))


def test_bel_location_valid():
    ok = {"A0": SliceUse(has_lut=True), "A1": SliceUse(has_lut=True, has_dff=True)}
    assert bel_location_valid(ok)
    bad = dict(ok, A1=SliceUse(has_dff=True))
    assert not bel_location_valid(bad)


def test_dff_semantics_preserved():
    mod = {"cells": {
        "d1": {"type": "DFF", "connections": {"D": "a", "CLK": "c", "Q": "q"},
               "parameters": {}, "attributes": {}},
        "d2": {"type": "DFFR", "connections": {"D": "a", "CLK": "c", "R": "r", "Q": "q2"},
               "parameters": {}, "attributes": {}},
        "d3": {"type": "DFFE", "connections": {"D": "a", "CLK": "c", "CE": "e", "Q": "q3"},
               "parameters": {}, "attributes": {}},
        "d4": {"type": "DFFNRE", "connections": {"D": "a", "CLK": "c", "R": "r",
                                                 "CE": "e", "Q": "q4"},
               "parameters": {}, "attributes": {}},
        "d5": {"type": "DFFCE", "connections": {"D": "a", "CLK": "c", "CE": "e",
                                                "CLEAR": "r", "Q": "q5"},
               "parameters": {}, "attributes": {}},
        "d6": {"type": "DFFPE", "connections": {"D": "a", "CLK": "c", "CE": "e",
                                                "PRESET": "s", "Q": "q6"},
               "parameters": {}, "attributes": {}},
    }}
    out = convert(mod)["cells"]
    assert out["d1"]["parameters"] == {}
    assert out["d2"]["parameters"]["RESET"] == "1"
    assert out["d2"]["parameters"]["RESET_POLARITY"] == "1"
    assert out["d3"]["parameters"]["CE"] == "1"
    assert out["d4"]["parameters"]["CLK_POLARITY"] == "0"
    # the clock edge is inverted in DFFN*, the reset sense is not
    assert out["d4"]["parameters"]["RESET_POLARITY"] == "1"
    assert "R" in out["d4"]["connections"] and "CE" in out["d4"]["connections"]
    assert out["d5"]["parameters"]["RESET"] == "1"
    assert out["d5"]["parameters"]["RESET_ASYNC"] == "1"   # DFFC is async
    assert out["d5"]["parameters"]["CE"] == "1"
    assert {"R", "CE", "D", "CLK", "Q"} <= set(out["d5"]["connections"])
    assert out["d6"]["parameters"]["SET"] == "1"
    assert out["d6"]["parameters"]["RESET_ASYNC"] == "1"   # DFFP is async
    assert out["d6"]["parameters"]["CE"] == "1"
    assert "S" in out["d6"]["connections"]


def test_constants_kept():
    mod = {"cells": {
        "v": {"type": "VCC", "connections": {"Y": "vcc"}, "parameters": {}, "attributes": {}},
        "g": {"type": "GND", "connections": {"Y": "gnd"}, "parameters": {}, "attributes": {}},
    }}
    out = convert(mod)["cells"]
    assert out["v"]["type"] == "VCC" and out["g"]["type"] == "GND"
    assert out["v"]["connections"]["Y"] == "vcc"
