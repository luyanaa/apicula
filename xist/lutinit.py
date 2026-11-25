"""LUT INIT: from the XPN slice function to fuse bits.

The XPN slice program carries the LUT function as a Boolean expression::

    K0::H0=(A*B)   K1::H1=(A*D)

with ``*`` AND, ``+`` OR, ``@`` XOR and ``~`` NOT.  To place that on the die we
need two per-tile measurements:

* **slot**: minterm -> (bit row, bit column).  Measured with five probes
  (:func:`xist.lutmap.calibrate_slice_fast`); the layout is an affine map of the
  minterm index, so minterm 0 plus the four basis minterms determine all 16.
* **varmap**: which minterm bit each LUT input (A/B/C/D) drives.  Measured by
  matching a known function against a vendor bitstream.

The BFD layout is a tile-type constant and directly usable: SRAM word ``addr``
lands at ``ram_row = base_row - (addr & 1)``, ``ram_col = base_col - (addr >> 1)``
for every PLC instance.  Minterm *j* of the truth table is SRAM word *j*.

Note on the old probe results: they suggested a per-tile permutation, but that
was the vendor re-encoding the function with a different A..D association per
tile (visible when reading the probe's own XPN, e.g. ``K0=(B*(A*(D*C)))`` for
``A*B*C*D``).  Evaluating the XPN's own expression with this identity map
reproduces the vendor bitstream exactly (21664/21664 bits over the corpus), so
no per-tile calibration is needed.
"""

from __future__ import annotations

import itertools
import json
import re
from pathlib import Path

from .bitstream import Bitstream, LutSlot

TESTDATA = Path(__file__).resolve().parent / "testdata"

_TOK_RE = re.compile(r"[A-D]|~|\*|\+|\@|\(|\)|[01]")


def eval_function(expr: str, values: dict[str, int]) -> int:
    """Evaluate a vendor slice expression (A..D, ~ * + @)."""
    toks = _TOK_RE.findall(expr)
    pos = 0

    def peek():
        return toks[pos] if pos < len(toks) else None

    def take():
        nonlocal pos
        t = toks[pos]
        pos += 1
        return t

    def primary():
        nonlocal pos
        t = peek()
        if t is None:
            raise ValueError(f"unexpected end of {expr!r}")
        if t == "~":
            take()
            return 1 - primary()
        if t == "(":
            take()
            v = or_expr()
            close = take()
            if close != ")":
                raise ValueError(f"expected ')' in {expr!r}, got {close!r}")
            return v
        if t in "ABCD":
            take()
            return int(values[t])
        if t in "01":
            take()
            return int(t)
        raise ValueError(f"bad token {t!r} in {expr!r}")

    def and_expr():
        v = primary()
        while peek() == "*":
            take()
            v = v & primary()
        return v

    def or_expr():
        v = and_expr()
        while peek() in ("+", "@"):
            op = take()
            rhs = and_expr()
            v = (v | rhs) if op == "+" else (v ^ rhs)
        return v

    out = or_expr()
    if pos != len(toks):
        raise ValueError(f"trailing tokens in {expr!r}")
    return out


def init_from_function(expr: str, varmap: dict[str, int]) -> int:
    """Truth table of *expr* as a 16-bit INIT using *varmap* (var -> minterm bit)."""
    init = 0
    for j in range(16):
        values = {v: (j >> b) & 1 for v, b in varmap.items()}
        if eval_function(expr, values):
            init |= 1 << j
    return init


PERMS = [dict(zip("ABCD", p)) for p in itertools.permutations(range(4))
         for p in [p]]


def varmap_candidates() -> list[dict[str, int]]:
    return [dict(zip("ABCD", p)) for p in itertools.permutations(range(4))]


def match_varmap(functions: dict[str, int], slots: dict[str, LutSlot],
                 bs: Bitstream, require_unique: bool = True) -> dict[str, int]:
    """Find the A..D -> minterm-bit map that reproduces the vendor INIT.

    *functions* maps bank (F0/F1) -> expression; *slots* maps bank -> slot.
    """
    hits = []
    for vm in varmap_candidates():
        ok = True
        for bank, expr in functions.items():
            init = init_from_function(expr, vm)
            slot = slots[bank]
            for j, (r, c) in enumerate(slot.minterms):
                want = "0" if (init >> j) & 1 else "1"
                if bs.rows[r][c] != want:
                    ok = False
                    break
            if not ok:
                break
        if ok:
            hits.append(vm)
    if not hits:
        raise RuntimeError("no varmap reproduces the bitstream")
    if require_unique and len(hits) > 1:
        raise RuntimeError(f"ambiguous varmap: {hits}")
    return hits[0]


def load_slots(path: str | Path | None = None) -> dict:
    if path is None:
        path = TESTDATA / "SL2-12E_lut_slots.json"
    return json.loads(Path(path).read_text())


def slot_for(slots: dict, loc: str) -> tuple[LutSlot, LutSlot]:
    """(F0, F1) slots for a slice location recorded in the calibration file."""
    if loc not in slots:
        raise KeyError(f"LUT slot for {loc} is not calibrated")
    d = slots[loc]
    f0 = LutSlot(tuple(tuple(p) for p in d["f0"]))
    f1 = LutSlot(tuple((r - 2, c) for r, c in d["f0"]))
    return f0, f1


def varmap_for(slots: dict, loc: str) -> dict[str, int]:
    d = slots[loc]
    return {k: int(v) for k, v in d["varmap"].items()}


def write_slice_luts(bs: Bitstream, slots: dict, loc: str,
                     functions: dict[str, str]) -> int:
    """Program the LUT INIT of one slice from its K0/K1 expressions."""
    from .bitstream import encode_lut_init
    if not functions:
        return 0
    f0, f1 = slot_for(slots, loc)
    vm = varmap_for(slots, loc)
    n = 0
    for bank, slot in (("F0", f0), ("F1", f1)):
        expr = functions.get(bank)
        if not expr:
            continue
        encode_lut_init(bs, init_from_function(expr, vm), slot)
        n += 16
    return n
