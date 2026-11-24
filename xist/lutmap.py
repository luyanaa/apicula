"""Calibrate LUT4 INIT fuse positions by differential probing.

For a locked SLICE location we synthesise 16 one-minterm LUTs (init = 1 << i).
Every design uses the same netlist; only the truth table changes.  Comparing each
probe with ``init = 1`` yields two changed fuses: minterm 0 (always) and minterm
*i*.  Intersecting all diffs isolates minterm 0, and the remaining position is
minterm *i*.  This recovers both the 2x8 block origin and the per-slice column
permutation.
"""

from __future__ import annotations

import functools
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


def calibrate_columns(part: str, tile_row: int, workdir: str | Path,
                      cols=None, probe_a: int = 1 << 11, probe_b: int = 1 << 12,
                      timeout: int = 900, verbose: bool = False) -> dict:
    """Calibrate tile column -> bitstream column origin.

    Uses slice D (the leftmost LUT block in a PLC tile).  Two probes whose
    minterms are at offsets 0 and 6 of the calibrated D layout give the block
    origin as the minimum changed column.
    """
    workdir = Path(workdir)
    cols = list(range(51) if cols is None else cols)
    band = (ROW_PITCH * tile_row - 8, ROW_PITCH * tile_row)
    out: dict[int, int | None] = {}
    for c in cols:
        loc = f"R{tile_row}C{c}D"
        try:
            a = run_lut_probe(part, workdir, [probe_a], f"col{c:02d}a", loc=loc,
                              timeout=timeout)
            b = run_lut_probe(part, workdir, [probe_b], f"col{c:02d}b", loc=loc,
                              timeout=timeout)
            d = diff_probes(a, b, band)
            pos = [(r, cc) for r, cs in d.items() for cc in cs]
            out[c] = min(cc for _, cc in pos) if pos else None
        except Exception as e:  # noqa: BLE001 - vendor errors are data
            out[c] = None
            if verbose:
                print(f"  C{c}: {e}")
        if verbose:
            print(f"  C{c}: origin={out[c]}")
    return out


# Column offset of each slice's LUT block inside a PLC tile, relative to slice D.
SLICE_BLOCK_OFFSET = {"D": 0, "C": 8, "B": 24, "A": 32}


def slot_for(cal: LutCalibration, tile_row: int, tile_col: int,
             columns: dict[int, int | None]) -> LutSlot:
    """Translate a calibration done at one tile to any tile.

    ``columns`` maps tile column -> slice-D block origin (see
    :func:`calibrate_columns`).  The per-slice minterm layout is reused as-is;
    only the row and column origins move.

    WARNING: the minterm permutation is **not** constant across the chip; this
    helper is exact only when *cal* was taken at the same tile position (or the
    vendor happens to use the same input mapping there).  Calibrate each tile
    before relying on it.
    """
    ref_row = _tile_row(cal.loc)
    ref_slice = cal.loc[-1]
    ref_origin = cal.block_col - SLICE_BLOCK_OFFSET[ref_slice]
    rel = [(r - ROW_PITCH * ref_row, c - cal.block_col)
           for r, c in (cal.minterms[i] for i in range(16))]
    origin = columns.get(tile_col) if isinstance(columns, dict) else None
    if origin is None:
        raise KeyError(f"no column origin for tile column {tile_col}")
    base = origin + SLICE_BLOCK_OFFSET[ref_slice]
    return LutSlot(tuple((ROW_PITCH * tile_row + dr, base + dc) for dr, dc in rel))


def calibrate_tiles(part: str, tiles: list[str], workdir: str | Path,
                    jobs: int = 1, timeout: int = 900) -> dict:
    """Calibrate many slice locations, optionally in parallel.

    Returns ``{loc: calibration_json}``.  A failed location gets ``{"error": ...}``
    so a long campaign can continue past unsupported tiles.
    """
    workdir = Path(workdir)
    fn = functools.partial(_calibrate_one, part, str(workdir), timeout)
    out: dict = {}
    if jobs <= 1:
        for loc in tiles:
            k, v = fn(loc)
            out[k] = v
        return out
    from concurrent.futures import ProcessPoolExecutor
    with ProcessPoolExecutor(max_workers=jobs) as ex:
        for k, v in ex.map(fn, tiles):
            out[k] = v
    return out


def _calibrate_one(part: str, workdir: str, timeout: int, loc: str):
    try:
        cal = calibrate_slice(part, loc, Path(workdir) / loc, timeout=timeout)
        return loc, cal.to_json()
    except Exception as e:  # noqa: BLE001
        return loc, {"error": str(e)}
