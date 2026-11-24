"""Routing-only fuse campaign: pin variants at many tile locations.

For each PLC tile location we run the same netlist (io_pass) with different
package-pin assignments while locking the two slices.  Bitstream bits are then
correlated with XPN pip presence (fit/holdout per location) and merged into a
pip -> fuse candidate table.  This is the scalable version of the first
R20C19 experiment.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import hqfpga
from .bitstream import Bitstream
from .corpus import DESIGNS
from .device import parse_udev_report
from .fusemap import correlate
from .xpn import parse_xpn

HERE = Path(__file__).resolve().parent
TESTDATA = HERE / "testdata"


def plc_locations(count: int) -> list[str]:
    rep = parse_udev_report((TESTDATA / "SL2-12E-8F256C_udev.txt").read_text())
    tiles = [(r, c) for r, row in enumerate(rep.tiles) for c, t in enumerate(row)
             if t == "PLC"]
    if count >= len(tiles):
        pick = tiles
    else:
        step = len(tiles) / count
        pick = [tiles[int(i * step)] for i in range(count)]
    return [f"R{r}C{c}" for r, c in pick]


def _run_one(part: str, design: str, loc: str, idx: int, pins: list[str],
             workdir: Path) -> tuple[str, int]:
    tag = f"{loc}_{idx}"
    off = (idx * 8) % len(pins)
    sel = [pins[(off + 7 * k) % len(pins)] for k in range(8)]
    a, b, c, d, o1, o2, o3, o4 = sel
    (workdir / f"{design}.v").write_text(DESIGNS[design])
    tcl = f"""dv.setup {_family(part)} {part}
design.analyze {{{design}.v}}
design.rtlsyn -top top
design.map
design.pack
phycst.start
phycst.pin.set a {a}
phycst.pin.set b {b}
phycst.pin.set c {c}
phycst.pin.set d {d}
phycst.pin.set o1 {o1}
phycst.pin.set o2 {o2}
phycst.pin.set o3 {o3}
phycst.pin.set o4 {o4}
phycst.loc.set SLICE_0 {loc}D
phycst.loc.set SLICE_1 {loc}C
phycst.end
design.place -seed 1
design.route
xpn.write {tag}.xpn
impl.bitgen.xist.{_bitgen(part)} {tag}.bit
exit
"""
    proc = hqfpga.run_script(tcl, str(workdir), log=f"{tag}.log")
    return tag, proc.returncode


def _family(part: str) -> str:
    from .device import family_for
    return family_for(part)


def _bitgen(part: str) -> str:
    return {"SEALION": "sealion", "SEAL": "seal", "SHARK": "shark"}.get(
        _family(part), "sealion")


def _validated(workdir: Path, loc: str, fit_ratio: float = 0.7) -> list[dict]:
    names = sorted(p.stem for p in workdir.glob(f"{loc}_*.bit")
                   if (workdir / f"{p.stem}.xpn").exists())
    if len(names) < 6:
        return []
    k = max(5, int(len(names) * fit_ratio))
    fit, hold = names[:k], names[k:]
    matches = correlate([workdir / f"{n}.bit" for n in fit],
                        [workdir / f"{n}.xpn" for n in fit])
    hd = []
    for n in hold:
        bs = Bitstream.read(str(workdir / f"{n}.bit"))
        ps = set()
        for sg in parse_xpn(workdir / f"{n}.xpn").values():
            ps.update(sg.route)
        hd.append((bs, ps))
    good = []
    for c in matches:
        pip = tuple(c["pip"])
        r, col = c["pos"]
        inv = c["invert"]
        if all((bs.rows[r][col] == "1") ==
               ((pip not in ps) if inv else (pip in ps)) for bs, ps in hd):
            good.append(c)
    return good


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="routing-only fuse campaign")
    ap.add_argument("--part", default="SL2-12E-8F256C")
    ap.add_argument("--design", default="io_pass")
    ap.add_argument("--locations", default="auto:8",
                    help="comma list or auto:N")
    ap.add_argument("--variants", type=int, default=8)
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--workdir", default="/tmp/xist_campaign")
    ap.add_argument("--out", default="pip_fuses.json")
    args = ap.parse_args(argv)
    wd = Path(args.workdir)
    wd.mkdir(parents=True, exist_ok=True)
    if args.locations.startswith("auto:"):
        locs = plc_locations(int(args.locations.split(":")[1]))
    else:
        locs = [x.strip() for x in args.locations.split(",") if x.strip()]
    pins = (TESTDATA / "SL2-12E-8F256C_pins.txt").read_text().split()

    jobs = [(loc, i) for loc in locs for i in range(args.variants)]
    with ThreadPoolExecutor(max_workers=args.jobs) as ex:
        results = list(ex.map(lambda li: _run_one(args.part, args.design,
                                                  li[0], li[1], pins, wd), jobs))
    ok = sum(1 for _, rc in results if rc == 0)
    print(f"runs ok: {ok}/{len(results)}")

    merged: dict[tuple[str, str], dict] = {}
    for loc in locs:
        for c in _validated(wd, loc):
            pip = tuple(c["pip"])
            merged.setdefault(pip, dict(c, source=f"{loc}_{args.design}"))
    out = {"note": f"routing-only campaign {args.design} at {len(locs)} locations",
           "device": args.part, "candidates": len(merged),
           "matches": [merged[k] for k in sorted(merged)]}
    Path(args.out).write_text(json.dumps(out, indent=1) + "\n")
    print(f"{len(merged)} candidates -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
