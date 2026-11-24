"""Install vendor BFD routing profiles into a himbaechel chipdb.

The chipdb format stores pips as directed (src, dst) wire pairs per tile type
and has no per-pip payload, so the implementing fuse positions travel in the
sidecar JSON written by :meth:`xist.chipdb.XistChip.write_meta`.

``install_profile`` adds every wire and arc of one BFD tile profile to a chipdb
tile type and returns ``arc -> fuse positions``.  The arcs are the vendor's
complete routing muxes (not the XPN-observed sample).
"""

from __future__ import annotations

import json
from pathlib import Path

from .dbgen.chip import Chip


def load_profile(path: str | Path) -> dict:
    return json.loads(Path(path).read_text())


def pip_fuses(profile: dict) -> dict[str, list[dict]]:
    """``"src->dst"`` -> list of ``{row, col, fuse}`` (tile-local Ram address)."""
    fuses = profile.get("fuses", {})
    out: dict[str, list[dict]] = {}
    for arc, names in profile.get("arc_fuses", {}).items():
        positions = []
        for name in names:
            for rc in fuses.get(name, []):
                positions.append(dict(row=rc[0], col=rc[1], fuse=name))
        if positions:
            out[arc] = positions
    return out


def _wire_names(tt) -> dict[int, str]:
    idx = {v: k for k, v in tt.strs.strs.items()}
    return {w.index: idx[w.name.index] for w in tt.wires}


def install_profile(chip: Chip, token: str, profile: dict) -> dict[str, list[dict]]:
    """Add a BFD profile's wires and pips to chipdb tile type *token*.

    Returns the arc -> fuse-position map for the installed pips.  Pips that
    already exist (bel-internal connections such as F -> DI) are kept.
    """
    if token not in chip.tile_type_idx:
        raise KeyError(f"no tile type {token!r}")
    tt = chip.tile_types[chip.tile_type_idx[token]]
    arcs = pip_fuses(profile)

    # create every endpoint wire first
    for arc in arcs:
        src, dst = arc.split("->", 1)
        for w in (src, dst):
            if not tt.has_wire(w):
                tt.create_wire(w)

    existing = set()
    for pip in tt.pips:
        existing.add((pip.src_wire, pip.dst_wire))
    by_name = _wire_names(tt)
    existing_named = {(by_name[s], by_name[d]) for s, d in existing}

    for arc in arcs:
        src, dst = arc.split("->", 1)
        if (src, dst) in existing_named:
            continue
        tt.create_pip(src, dst)
        existing_named.add((src, dst))
    return arcs
