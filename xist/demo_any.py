#!/usr/bin/env python3
"""Route an arbitrary small Verilog design with the XIST bring-up graph.

Flow: Yosys (synth_gowin JSON) -> XIST LUT4/DFF/ALU/PIO cells -> chipdb
(built from the device exports, no vendor routing corpus) -> nextpnr-himbaechel.

The routing graph is a bring-up model: a per-tile CIB crossbar plus a complete
1-row long-wire mesh.  It is not yet the vendor fuse map, but it lets arbitrary
LUT/DFF/ALU/IO designs place and route through nextpnr.

Requirements: yosys in PATH, nextpnr-himbaechel built with the xist uarch.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TESTDATA = HERE / "testdata"


def run(cmd, **kw):
    print("+", " ".join(str(c) for c in cmd))
    return subprocess.run([str(c) for c in cmd], capture_output=True,
                          text=True, **kw)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("verilog", nargs="?", default=str(TESTDATA / "io.v"))
    ap.add_argument("--top", default="top")
    ap.add_argument("--nextpnr", default=str(Path.home() /
                    "Downloads/nextpnr/build/nextpnr-himbaechel"))
    ap.add_argument("--workdir", default="/tmp/xist_any_demo")
    ap.add_argument("--device", default="SL2-12E")
    args = ap.parse_args(argv)
    wd = Path(args.workdir)
    wd.mkdir(parents=True, exist_ok=True)

    # 1. Yosys generic LUT/DFF netlist
    r = run(["yosys", "-q", "-p",
             f"read_verilog {args.verilog}; synth_gowin -json {wd / 'design.json'}"])
    if r.returncode:
        print(r.stderr)
        return r.returncode

    # 2. Convert to XIST cells
    from xist.nextpnr_json import convert
    data = json.loads((wd / "design.json").read_text())
    top = next(k for k, m in data["modules"].items()
               if "top" in m.get("attributes", {}))
    data["modules"] = {top: convert(data["modules"][top])}
    (wd / "design_xist.json").write_text(json.dumps(data, indent=1) + "\n")

    # 3. Build the chipdb (no XPN corpus: the CIB/long-wire model is enough)
    from xist.device import parse_udev_report, parse_pkg_report
    from xist.chipdb import build_chip
    from xist.bbasm import assemble
    from xist.lutmap import load
    rep = parse_udev_report((TESTDATA / "SL2-12E-8F256C_udev.txt").read_text())
    pkg = parse_pkg_report((TESTDATA / "SL2-12E-8F256C_pkg.txt").read_text())
    chip = build_chip(
        "SL2-12E-8F256C", rep,
        luts={"A": load(TESTDATA / "SL2-12E_lut_A.json")},
        xpn_paths=[], pkg_pins=pkg,
        columns=json.loads((TESTDATA / "SL2-12E_columns.json").read_text()),
        constids_path=HERE / "nextpnr/uarch/xist/constids.inc")
    bba = wd / "xist-SL2-12E-8F256C.bba"
    chip.write_bba(bba)
    (wd / "xist-SL2-12E-8F256C.bin").write_bytes(assemble(bba.read_text()))

    # 4. nextpnr: place + route
    r = run([args.nextpnr, "--device", args.device,
             "--chipdb", wd / "xist-SL2-12E-8F256C.bin",
             "--json", wd / "design_xist.json",
             "--no-tmdriv", "--write", wd / "routed.json"])
    if r.returncode:
        print(r.stdout[-2000:])
        print(r.stderr[-2000:])
        return r.returncode
    for line in (r.stdout + r.stderr).splitlines():
        if "Routing" in line or "Program finished" in line or "ERROR" in line:
            print(line)
    routed = json.loads((wd / "routed.json").read_text())
    nets = sum(1 for n in routed["modules"][top]["netnames"].values()
               if n.get("attributes", {}).get("ROUTING"))
    print(f"Routing complete: {nets} nets with ROUTING")
    return 0


if __name__ == "__main__":
    sys.exit(main())
