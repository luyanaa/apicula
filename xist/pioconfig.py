"""Model the edge PIO configuration from BFD ``property()``/``config()`` rules.

The 59-column edge tile types contain pure configuration equations, e.g.::

    MC12_RD_A    = ... && config(PIO_A,PADDI,"PADDI")
    MC12_PG2_N_A = ... property(PIO_A,IOBUF.PULLMODE,"UP") ...
    MC12_CLAMP_A = property(PIO_A,IOBUF.CLAMP,"ON") && config(PIO_A,PADDI,"PADDI")

The predicates refer to the BFD *site* name (``PIO_A``), not the placement site
(``PL2A``); the last letter of the placement site selects the PIO site.  The
program string in the XPN component carries the values::

    PADDI:PADDI VREF:OFF PGMUX:INBUF INRDMUX:PGMUX
    IOBUF:::PULLMODE=DOWN,IO_TYPE=LVCMOS25,CLAMP=ON,...

This module parses those two sides and evaluates every pure-config fuse for a
tile, so the bitstream writer can emit PIO configuration from the routed
netlist instead of guessing from probe patterns.
"""

from __future__ import annotations

import re

from pathlib import Path

from .bfd import BfdTile
from .bfd_expr import evaluate, parse_cached
from .xpn import Comp

_FIELD_RE = re.compile(r"([A-Z_][A-Z0-9_]*)=([^,]*)")


def parse_program(program: str) -> tuple[dict[str, str], set[tuple[str, str]]]:
    """Split an XPN program string into property fields and port configs."""
    props: dict[str, str] = {}
    configs: set[tuple[str, str]] = set()
    for tok in program.split():
        tok = tok.strip(",")
        if "=" in tok:
            for key, value in _FIELD_RE.findall(tok):
                props[key] = value
        elif ":" in tok:
            port, mode = tok.split(":", 1)
            configs.add((port, mode))
    return props, configs


def pio_site(site: str | None) -> str | None:
    """PL2A -> PIO_A; returns None for non-PIO sites."""
    if not site or site[-1] not in "ABCD":
        return None
    return f"PIO_{site[-1]}"


def env_for(comps: list[Comp]) -> dict:
    """Build a BFD expression environment for all PIO comps of one tile."""
    props: dict[tuple[str, str], str] = {}
    configs: dict[str, set[tuple[str, str]]] = {}
    sites: set[str] = set()
    for comp in comps:
        site = pio_site(comp.site)
        if site is None:
            continue
        sites.add(site)
        fields, cfgs = parse_program(comp.program)
        for key, value in fields.items():
            props[(site, f"IOBUF.{key}")] = value
        configs.setdefault(site, set()).update(cfgs)
    return {"props": props, "configs": configs, "sites": sites}


def config_equations(tile: BfdTile) -> list[tuple[str, object, list]]:
    """Pure configuration fuses (no arcval term)."""
    out = []
    for name, expr in tile.equations.items():
        if "arcval" in expr:
            continue
        if "property(" not in expr and "config(" not in expr:
            continue
        out.append((name, parse_cached(expr), tile.fuse_positions(name)))
    return out


def evaluate_config(tile: BfdTile, comps: list[Comp],
                    equations=None) -> dict[str, bool]:
    """Fuse name -> value for every pure-config fuse of the tile."""
    env = env_for(comps)
    eqs = config_equations(tile) if equations is None else equations
    return {name: evaluate(ast, env) for name, ast, _ in eqs}


def bits_from_equations(eqs, comps: list[Comp], tile_row: int, col_base: int,
                        row_base: int | None = None
                        ) -> list[tuple[int, int, bool]]:
    """Absolute ``(bit_row, bit_col, value)`` from a stored equation list.

    ``row_base`` is 1108 for the top edge and 1102 for the bottom edge; when
    omitted the regular 29-row pitch is used.
    """
    env = env_for(comps)
    rbase = row_base if row_base is not None else 29 * (tile_row - 1)
    out = []
    for name, ast, positions in eqs:
        value = evaluate(ast, env)
        for (ram_row, ram_col) in positions:
            out.append((rbase + ram_row, col_base - ram_col, value))
    return out


def pio_bits(tile: BfdTile, comps: list[Comp], tile_row: int, col_base: int,
             equations=None) -> list[tuple[int, int, bool]]:
    """Absolute ``(bit_row, bit_col, value)`` for the tile's PIO config."""
    eqs = config_equations(tile) if equations is None else equations
    return bits_from_equations(eqs, comps, tile_row, col_base)


def load_profile(path: str | Path, name: str):
    """Load a stored PIO profile -> (equations, metadata)."""
    import json
    data = json.loads(Path(path).read_text())["tile_types"][name]
    eqs = [(fuse, parse_cached(info["expr"]),
            [tuple(p) for p in info["positions"]])
           for fuse, info in data["config_fuses"].items()]
    return eqs, data


def main(argv=None) -> int:
    import argparse
    import json
    from pathlib import Path
    from .bfd import parse as parse_bfd
    from .bitstream import Bitstream
    from .xpn import parse_comps

    ap = argparse.ArgumentParser(description="evaluate edge PIO config fuses")
    ap.add_argument("--bfd", required=True)
    ap.add_argument("--tile", default="PIC_L5")
    ap.add_argument("--xpn", required=True)
    ap.add_argument("--row", type=int, required=True)
    ap.add_argument("--base", type=int, required=True)
    ap.add_argument("--bit", default=None)
    args = ap.parse_args(argv)
    tile = parse_bfd(args.bfd)[args.tile]
    comps = [c for c in parse_comps(args.xpn).values()
             if pio_site(c.site) and c.site and c.site[2:3].isdigit()]
    bits = pio_bits(tile, comps, args.row, args.base)
    print(f"{len(bits)} config bits for tile {args.tile} row {args.row}")
    bs = Bitstream.read(args.bit) if args.bit else None
    ok = 0
    for br, bc, val in bits:
        if not val:
            continue
        actual = bs.rows[br][bc] == "1" if bs else None
        ok += bool(actual)
        print(f"  ({br},{bc}) = {int(val)}" + (f" actual {int(actual)}" if bs else ""))
    if bs:
        print(f"set bits matching: {ok}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
