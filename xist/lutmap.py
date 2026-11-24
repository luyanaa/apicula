"""Calibrate LUT4 INIT fuse positions by differential probing.

For a locked SLICE location we synthesise 16 one-minterm LUTs (init = 1 << i).
Every design uses the same netlist; only the truth table changes.  Comparing each
probe with ``init = 1`` yields two changed fuses: minterm 0 (always) and minterm
*i*.  Intersecting all diffs isolates minterm 0, and the remaining position is
minterm *i*.  This recovers both the 2x8 block origin and the per-slice column
permutation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .bitstream import Bitstream, LutSlot, ROW_PITCH, decode_lut_init, encode_lut_init
from .probe import ProbeResult, diff_probes, lut_port, run_lut_probe


@dataclass
class LutCalibration:
    """Minterm -> (bitmap row, bitmap col) for one LUT slot."""
    part: str
    loc: str
    minterms: dict[int, tuple[int, int]]
    extra: dict = field(default_factory=dict)

    @property
    def rows(self) -> tuple[int, ...]:
        return tuple(sorted({r for r, _ in self.minterms.values()}))

    @property
    def block_col(self) -> int:
        return min(c for _, c in self.minterms.values())

    @property
    def perm(self) -> tuple[tuple[int, int], ...]:
        """(row, col) for minterms 0..15, normalised by block_col."""
        base = self.block_col
        return tuple((r, c - base) for r, c in
                     (self.minterms[i] for i in range(16)))

    def slot(self) -> LutSlot:
        return LutSlot(tuple(self.minterms[i] for i in range(16)))

    def to_json(self) -> dict:
        return dict(part=self.part, loc=self.loc,
                    minterms={str(k): list(v) for k, v in sorted(self.minterms.items())},
                    rows=list(self.rows), block_col=self.block_col,
                    extra=self.extra)

    @classmethod
    def from_json(cls, d: dict) -> "LutCalibration":
        return cls(d["part"], d["loc"],
                   {int(k): tuple(v) for k, v in d["minterms"].items()},
                   d.get("extra", {}))


def _tile_row(loc: str) -> int:
    # R10C10A / R20C19D
    return int(loc[1:loc.index("C")])


def calibrate_slice(part: str, loc: str, workdir: str | Path,
                    timeout: int = 900, verbose: bool = False) -> LutCalibration:
    """Calibrate the F0 LUT of the slice pinned at *loc*."""
    workdir = Path(workdir)
    probes: dict[int, ProbeResult] = {}
    for i in range(16):
        probes[i] = run_lut_probe(part, workdir, [1 << i], f"lut_{loc}_{i:02d}",
                                  loc=loc, timeout=timeout)
        if verbose:
            print(f"  probe {i:02d} rc={probes[i].returncode}")
    r = _tile_row(loc)
    band = (ROW_PITCH * r - 8, ROW_PITCH * r)
    diffs = {i: diff_probes(probes[0], probes[i], band) for i in range(1, 16)}

    # positions minus the checksum / footer rows are (row, col)
    pos = {i: {(row, c) for row, cols in d.items() for c in cols}
           for i, d in diffs.items()}

    # minterm 0 changes in every diff; intersect.
    common = set.intersection(*pos.values()) if pos else set()
    if len(common) != 1:
        raise RuntimeError(f"{loc}: cannot isolate minterm 0 (common={sorted(common)})")
    bit0 = next(iter(common))
    minterms = {0: bit0}
    for i in range(1, 16):
        others = pos[i] - {bit0}
        # a clean probe has exactly one other position
        if len(others) != 1:
            raise RuntimeError(
                f"{loc}: minterm {i} produced {len(others)} candidate fuses: "
                f"{sorted(others)} (the vendor synth may have permuted inputs)")
        minterms[i] = next(iter(others))

    cal = LutCalibration(part, loc, minterms,
                         extra=dict(probe="single LUT, bare xsLUT4",
                                    port=lut_port(probes[0].phy_verilog)))
    return cal


def verify_calibration(cal: LutCalibration, part: str, workdir: str | Path,
                       init: int = 0x6996, timeout: int = 900) -> bool:
    """Re-probe an arbitrary INIT and compare with the encoded fuses."""
    workdir = Path(workdir)
    ref = run_lut_probe(part, workdir, [init], f"verify_{cal.loc}_ref",
                        loc=cal.loc, timeout=timeout)
    other = run_lut_probe(part, workdir, [init ^ 0xFFFF], f"verify_{cal.loc}_inv",
                          loc=cal.loc, timeout=timeout)
    slot = cal.slot()
    # encode `init` into a copy of the reference bitstream: every fuse must match
    bs = Bitstream.read(str(workdir / f"verify_{cal.loc}_ref.bit"))
    encode_lut_init(bs, init, slot)
    return bs.to_bytes() == (workdir / f"verify_{cal.loc}_ref.bit").read_bytes()


def save(cal: LutCalibration, path: str | Path) -> None:
    Path(path).write_text(json.dumps(cal.to_json(), indent=2) + "\n")


def load(path: str | Path) -> LutCalibration:
    return LutCalibration.from_json(json.loads(Path(path).read_text()))
