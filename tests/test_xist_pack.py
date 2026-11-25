"""End-to-end pack check: every placed DFF is clustered with its bank LUT.

The packer lives in the nextpnr uarch (``xist.cc``), so this test runs the real
flow (yosys -> converter -> chipdb -> nextpnr) and inspects the routed netlist.
It is skipped when yosys or nextpnr-himbaechel is not available.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

APICULA = Path(__file__).resolve().parent.parent
NEXTPNR = Path.home() / "Downloads/nextpnr/build/nextpnr-himbaechel"

pytestmark = pytest.mark.skipif(
    shutil.which("yosys") is None or not NEXTPNR.exists(),
    reason="needs yosys and a built nextpnr-himbaechel",
)


def test_shift_register_packs_lut_and_dff(tmp_path):
    src = tmp_path / "shift4.v"
    src.write_text(
        "module top(input clk, input si, output reg [3:0] q);\n"
        "  always @(posedge clk) q <= {q[2:0], si};\n"
        "endmodule\n"
    )
    from xist.demo_any import main

    wd = tmp_path / "w"
    rc = main([str(src), "--bfd-replace", "--workdir", str(wd),
               "--nextpnr", str(NEXTPNR)])
    assert rc == 0

    routed = json.loads((wd / "routed.json").read_text())
    cells = next(iter(routed["modules"].values()))["cells"]
    tiles: dict[str, set[str]] = {}
    for cell in cells.values():
        if cell["type"] not in ("LUT4", "DFF"):
            continue
        tile, bel = cell["attributes"]["NEXTPNR_BEL"].split("/")
        tiles.setdefault(tile, set()).add(bel)

    assert tiles, "no LUT/DFF cells were placed"
    for tile, bels in tiles.items():
        for bel in bels:
            if bel.startswith("DFF"):
                # the DFF's D input is the slice output mux, fed by the same
                # bank's LUT: both must sit in one tile
                assert f"LUT{bel[-1]}" in bels, f"{tile}/{bel} lost its LUT"
