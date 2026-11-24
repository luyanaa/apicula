"""Command line entry points for the XIST database tooling.

Examples
--------
    python -m xist.cli udev --part SL2-12E-8F256C --out /tmp/xist
    python -m xist.cli calibrate --part SL2-12E-8F256C --loc R10C10A --out /tmp/xist
    python -m xist.cli chipdb --part SL2-12E-8F256C --xpn a.xpn --out-bba xist.bba
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import hqfpga
from .bitstream import Bitstream, LutSlot
from .chipdb import build_chip
from .device import family_for, parse_pkg_report, parse_udev_report
from .lutmap import calibrate_slice, load as load_cal, save as save_cal
from .probe import run_lut_probe


def cmd_udev(args) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    fam = family_for(args.part)
    script = "\n".join([
        f"dv.setup {fam} {args.part}",
        "puts \"@@UDEV@@\"",
        "'udev.report",
        "puts \"@@IOTYPE@@\"",
        "'udev.iotype.report",
        f"'udev.pkg.report -file {{{out / (args.part + '_pkg.txt')}}}",
        "exit", ""])
    proc = hqfpga.run_script(script, str(out), log="udev.log")
    sys.stdout.write(proc.stdout)
    if proc.returncode != 0:
        return proc.returncode
    text = proc.stdout.split("@@UDEV@@", 1)[1]
    rep = parse_udev_report(text)
    (out / f"{args.part}_arch.txt").write_text(text)
    (out / f"{args.part}_arch.json").write_text(json.dumps({
        "rows": rep.rows, "cols": rep.cols, "tiles": rep.tiles,
        "sites": rep.sites, "counts": rep.counts,
    }, indent=2) + "\n")
    print(f"grid {rep.rows}x{rep.cols}, tile types: {rep.tile_type_counts()}")
    pkg = out / f"{args.part}_pkg.txt"
    if pkg.is_file():
        pins = parse_pkg_report(pkg.read_text(encoding="utf-8", errors="replace"))
        (out / f"{args.part}_pkg.json").write_text(json.dumps(pins, indent=2) + "\n")
        print(f"{len(pins)} package pins")
    return 0


def cmd_probe(args) -> int:
    res = run_lut_probe(args.part, args.out, [int(args.init, 0)], args.name,
                        loc=args.loc)
    print(f"{res.name}: rc={res.returncode}")
    if res.bit:
        print(f"{res.bit.nrows}x{res.bit.ncols}")
    return 0 if res.returncode == 0 else 1


def cmd_calibrate(args) -> int:
    cal = calibrate_slice(args.part, args.loc, args.out, verbose=args.verbose)
    print(f"{args.loc}: rows={cal.rows} block_col={cal.block_col} perm={cal.perm}")
    dest = Path(args.save or Path(args.out) / f"lut_{args.loc}.json")
    save_cal(cal, dest)
    print(f"saved {dest}")
    return 0


def cmd_chipdb(args) -> int:
    rep = parse_udev_report(Path(args.udev).read_text(encoding="utf-8"))
    luts = {}
    for p in args.lut or []:
        cal = load_cal(p)
        luts[cal.loc[-1]] = cal
    chip = build_chip(args.part, rep, luts=luts,
                      xpn_paths=args.xpn or [], pkg_pins=[])
    out = Path(args.out_bba)
    out.parent.mkdir(parents=True, exist_ok=True)
    chip.write_bba(out)
    if args.out_meta:
        chip.write_meta(args.out_meta)
    print(f"wrote {out} ({len(chip.chip.tile_types)} tile types, "
          f"{sum(len(t.bels) for t in chip.chip.tile_types)} bels)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="xist")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("udev", help="export tile grid + package pins")
    p.add_argument("--part", required=True)
    p.add_argument("--out", default=".")
    p.set_defaults(func=cmd_udev)

    p = sub.add_parser("probe", help="run one LUT INIT probe")
    p.add_argument("--part", required=True)
    p.add_argument("--init", default="0x1")
    p.add_argument("--loc", default=None)
    p.add_argument("--name", default="probe")
    p.add_argument("--out", default=".")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("calibrate", help="calibrate one slice's F0 LUT")
    p.add_argument("--part", required=True)
    p.add_argument("--loc", required=True)
    p.add_argument("--out", default=".")
    p.add_argument("--save", default=None)
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(func=cmd_calibrate)

    p = sub.add_parser("chipdb", help="emit a himbaechel .bba chipdb")
    p.add_argument("--part", required=True)
    p.add_argument("--udev", required=True, help="'udev.report text file")
    p.add_argument("--lut", action="append", help="LUT calibration JSON")
    p.add_argument("--xpn", action="append", help="routed XPN file(s)")
    p.add_argument("--out-bba", required=True)
    p.add_argument("--out-meta", default=None)
    p.set_defaults(func=cmd_chipdb)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
