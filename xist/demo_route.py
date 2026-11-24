#!/usr/bin/env python3
"""End-to-end demo: Yosys -> XIST cells -> nextpnr-himbaechel route.

This reproduces the first successfully routed XIST design (`a & b -> o`) against
a local nextpnr checkout and the `xist/` database tooling.

Requirements:
  * yosys in PATH
  * nextpnr-himbaechel built with -DARCH=himbaechel -DHIMBAECHEL_UARCH=xist
    (default path: ~/Downloads/nextpnr/build/nextpnr-himbaechel)
  * the fixture files in xist/testdata (udev grid, package pins, LUT map,
    column origins and io.xpn)
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TESTDATA = HERE / "testdata"
ROOT = HERE.parent


def run(cmd, **kw):
    print("+", " ".join(str(c) for c in cmd))
    return subprocess.run([str(c) for c in cmd], check=False,
                          capture_output=True, text=True, **kw)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--nextpnr", default=str(Path.home() /
                    "Downloads/nextpnr/build/nextpnr-himbaechel"))
    ap.add_argument("--workdir", default="/tmp/xist_route_demo")
    ap.add_argument("--device", default="SL2-12E")
    args = ap.parse_args(argv)
    wd = Path(args.workdir)
    wd.mkdir(parents=True, exist_ok=True)

    # 1. Yosys: generic Gowin LUT/DFF netlist (used only as an input format)
    r = run(["yosys", "-q", "-p",
             f"read_verilog {TESTDATA / 'io.v'}; synth_gowin -json {wd / 'io.json'}"])
    if r.returncode:
        print(r.stderr)
        return r.returncode

    # 2. Convert to XIST cells and constrain the demo placement
    from xist.nextpnr_json import convert
    data = json.loads((wd / "io.json").read_text())
    top = next(k for k, m in data["modules"].items()
               if "top" in m.get("attributes", {}))
    mod = convert(data["modules"][top])
    ports = mod["ports"]
    # pad net bit -> top-level port name
    bit_port = {}
    for name, p in ports.items():
        for b in p["bits"]:
            bit_port[b] = name
    slot = {"a": "A", "b": "B", "o": "C"}
    for cell in mod["cells"].values():
        if cell["type"] == "PIO":
            pad = cell["connections"].get("PAD", [None])[0]
            name = bit_port.get(pad)
            if name in slot:
                cell.setdefault("attributes", {})["BEL"] = f"X19Y39/PIO_{slot[name]}"
        elif cell["type"] == "LUT4":
            cell.setdefault("attributes", {})["BEL"] = "X19Y20/LUT6"
    data["modules"] = {top: mod}
    (wd / "io_xist.json").write_text(json.dumps(data, indent=1) + "\n")

    # 3. Build the chipdb from the matching routed XPN and assemble it
    from xist.device import parse_udev_report, parse_pkg_report
    from xist.chipdb import build_chip
    from xist.bbasm import assemble
    from xist.lutmap import load

    rep = parse_udev_report((TESTDATA / "SL2-12E-8F256C_udev.txt").read_text())
    pkg = parse_pkg_report((TESTDATA / "SL2-12E-8F256C_pkg.txt").read_text())
    chip = build_chip(
        "SL2-12E-8F256C", rep,
        luts={"A": load(TESTDATA / "SL2-12E_lut_A.json")},
        xpn_paths=[str(TESTDATA / "io.xpn")],
        pkg_pins=pkg,
        columns=json.loads((TESTDATA / "SL2-12E_columns.json").read_text()),
        constids_path=HERE / "nextpnr/uarch/xist/constids.inc")
    bba = wd / "xist-SL2-12E-8F256C.bba"
    chip.write_bba(bba)
    (wd / "xist-SL2-12E-8F256C.bin").write_bytes(
        assemble(bba.read_text()))

    # 4. nextpnr: place + route
    r = run([args.nextpnr, "--device", args.device,
             "--chipdb", wd / "xist-SL2-12E-8F256C.bin",
             "--json", wd / "io_xist.json",
             "--no-tmdriv", "--write", wd / "io_routed.json"])
    tail = "\n".join(r.stdout.splitlines()[-8:])
    print(tail)
    if r.returncode != 0:
        print(r.stderr)
        return r.returncode
    routed = json.loads((wd / "io_routed.json").read_text())
    arcs = 0
    for net in routed["modules"][top]["netnames"].values():
        if net.get("attributes", {}).get("ROUTING"):
            arcs += 1
    print(f"routed nets with ROUTING: {arcs}")
    return 0 if arcs >= 3 else 1


if __name__ == "__main__":
    sys.exit(main())
