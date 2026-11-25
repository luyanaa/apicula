#!/usr/bin/env python3
"""Route an arbitrary small Verilog design with the XIST bring-up graph.

Flow: Yosys (synth_gowin JSON) -> XIST LUT4/DFF/ALU/PIO cells -> chipdb
(built from the device exports, no vendor routing corpus) -> nextpnr-himbaechel.

The routing graph is a bring-up model: a per-tile CIB crossbar plus a complete
1-row long-wire mesh.  Pass ``--bfd`` to add the vendor BFD routing pips for
PLC tiles (and ``--bfd-replace`` to drop the synthetic PLC crossbar).  With
``--bfd-replace`` a LUT/IO design routes through vendor fuse-backed pips only;
DFF-heavy designs still need proper LUT/DFF packing (a DFF data input is fed by
the same slice's output mux, not by a free routing arc).

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
    ap.add_argument("--bfd", action="store_true",
                    help="use the vendor BFD PLC routing profile")
    ap.add_argument("--bfd-replace", action="store_true",
                    help="drop the generic CIB for PLC (BFD only)")
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
    from xist.chipdb import build_chip, default_bfd_profiles
    from xist.bbasm import assemble
    from xist.lutmap import load
    rep = parse_udev_report((TESTDATA / "SL2-12E-8F256C_udev.txt").read_text())
    pkg = parse_pkg_report((TESTDATA / "SL2-12E-8F256C_pkg.txt").read_text())
    bfd = None
    bitcol = None
    if args.bfd or args.bfd_replace:
        bfd = default_bfd_profiles()
        bitcol = {int(k): v for k, v in json.loads(
            (TESTDATA / "SL2-12E_bitcol_base.json").read_text()
        )["column_base"].items()}
    chip = build_chip(
        "SL2-12E-8F256C", rep,
        luts={"A": load(TESTDATA / "SL2-12E_lut_A.json")},
        xpn_paths=[], pkg_pins=pkg,
        columns=json.loads((TESTDATA / "SL2-12E_columns.json").read_text()),
        constids_path=HERE / "nextpnr/uarch/xist/constids.inc",
        bfd_profiles=bfd, replace_cib=args.bfd_replace,
        bitcol_base=bitcol)
    bba = wd / "xist-SL2-12E-8F256C.bba"
    chip.write_bba(bba)
    if chip.routing_fuses:
        chip.write_meta(wd / "xist-SL2-12E-8F256C.meta.json")
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
