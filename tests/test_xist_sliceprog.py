"""Slice-program synthesis, checked against vendor XPN exports.

Two small designs were run through the vendor tool (HqFpga 3.1.0) to capture the
program the packer writes for the interesting DFF cases:

* ``SL2-12E_dff_lut.xpn``   - registered LUT output (D from the slice LUT, SD=1)
* ``SL2-12E_dff_route.xpn`` - shift register (D from the routed M input, SD=0)

``counter.xpn`` (already in testdata) covers the synchronous-reset + CE case.
The synthesised program must reproduce the vendor's configuration and property
fields exactly; the LUT functions are passed through and not compared.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from xist.sliceconfig import parse_slice_program
from xist.sliceprog import SliceFacts, slice_program
from xist.xpn import parse_comps

TESTDATA = Path(__file__).resolve().parent.parent / "xist" / "testdata"


def _config_fields(program: str):
    props, configs, _funcs = parse_slice_program(program)
    return props, configs


@pytest.mark.parametrize("path,facts", [
    ("SL2-12E_dff_lut.xpn", {
        "R20C19D": SliceFacts(luts=(0, 1), dffs=(0,)),
    }),
    ("SL2-12E_dff_route.xpn", {
        "R20C19D": SliceFacts(dffs=(0,), sd_from_lut=False),
        "R20C19C": SliceFacts(dffs=(0,), sd_from_lut=False),
    }),
    ("counter.xpn", {
        "R20C19D": SliceFacts(luts=(0, 1), dffs=(0,), reset=True,
                              reset_is_async=False, ce=True),
        "R20C19C": SliceFacts(luts=(0, 1), dffs=(0, 1), reset=True,
                              reset_is_async=False, ce=True),
        "R20C19B": SliceFacts(luts=(0,), dffs=(0,), reset=True,
                              reset_is_async=False, ce=True),
    }),
])
def test_program_matches_vendor(path, facts):
    comps = parse_comps(TESTDATA / path)
    seen = 0
    for comp in comps.values():
        if not comp.site or comp.site not in facts:
            continue
        vendor = _config_fields(comp.program)
        synth = _config_fields(slice_program(facts[comp.site]))
        assert synth == vendor, comp.site
        seen += 1
    assert seen == len(facts)


def test_lut_only_slice_has_no_register_fields():
    program = slice_program(SliceFacts(luts=(0,)))
    props, configs = _config_fields(program)
    assert props == {}
    assert configs == {("MODE", "LOGIC")}


def test_route_data_uses_sd0_and_m0mux():
    program = slice_program(SliceFacts(dffs=(0,), sd_from_lut=False))
    _props, configs = _config_fields(program)
    assert ("REG0.SD", "0") in configs
    assert ("REGMODE", "FF") in configs
    # the M input mux is a site setting, outside the BFD slice equations
    assert "M0MUX:M0" in program


def test_lut_data_uses_sd1_without_m0mux():
    _props, configs = _config_fields(slice_program(SliceFacts(dffs=(0,))))
    assert ("REG0.SD", "1") in configs
    assert not any(k.startswith("M0MUX") for k, _v in configs)


def test_async_and_sync_reset_differ():
    async_p = slice_program(SliceFacts(dffs=(0,), reset=True, reset_is_async=True))
    sync_p = slice_program(SliceFacts(dffs=(0,), reset=True, reset_is_async=False))
    assert ("SRMODE", "ASYNC") in _config_fields(async_p)[1]
    assert ("SRMODE", "LSR_OVER_CE") in _config_fields(sync_p)[1]
