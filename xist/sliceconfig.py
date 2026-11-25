"""Evaluate the slice (DFF/ALU/LUT control) configuration from the BFD.

The XPN slice component carries a program such as::

    MODE:LOGIC K1::H1=(A*D) K0::H0=(A*B)
    REG0:::REGSET=RESET,CLKDELAY=DEL0:SD=1
    Q0:Q F1:F F0:F GSR:ENABLED
    CLKMUX:CLK:::0=0,CLK=#SIG CEMUX:1:::1=1,CE=#SIG
    SRMODE:ASYNC LSRONMUX:0 REGMODE:FF

The PLC BFD has pure configuration equations over these fields, e.g.::

    MC1_SEL_0 = config(SLICE_A,REG0.SD,"SIG")
    MC1_SR_0  = property(SLICE_A,REG0.REGSET,"RESET")
    MC1_PICLK = config(SLICE_A,REGMODE,"FF") && ...

This module parses the program, maps it to the BFD site name ``SLICE_A..D``
and evaluates every pure-configuration fuse.  Validated bit-exact on the DFF
probe and on the routed corpus.
"""

from __future__ import annotations

import re

from .bfd import BfdTile
from .bfd_expr import evaluate, parse_cached
from .xpn import Comp

_SCALARS = ("MODE", "REGMODE", "SRMODE", "GSR", "LSRONMUX")


def slice_site(site: str | None) -> str | None:
    """Placement site R20C19D -> BFD site SLICE_D; None otherwise."""
    if not site or site[-1] not in "ABCD":
        return None
    if not (site.startswith("R") or site.startswith("P")):
        return None
    return f"SLICE_{site[-1]}"


def parse_slice_program(program: str):
    """Return ``(props, configs, functions)`` for one slice program.

    props: (field) -> value, e.g. REG0.REGSET -> RESET
    configs: set of (port, value), e.g. (REGMODE, FF), (REG0.SD, 1)
    functions: K0/K1 -> expression of A..D
    """
    props: dict[str, str] = {}
    configs: set[tuple[str, str]] = set()
    functions: dict[str, str] = {}
    for tok in program.split():
        parts = tok.split(":")
        key = parts[0]
        if key in ("K0", "K1", "K2", "K3"):
            for grp in parts[1:]:
                if "=" in grp:
                    functions[key] = grp.split("=", 1)[1]
            continue
        if key in _SCALARS:
            if len(parts) > 1 and parts[1]:
                configs.add((key, parts[1]))
            continue
        if key.startswith("REG"):
            for grp in parts[1:]:
                for field in re.split(r"[,:]", grp):
                    if "=" not in field:
                        continue
                    k, v = field.split("=", 1)
                    if k in ("REGSET", "CLKDELAY", "REGMODE"):
                        props[f"{key}.{k}"] = v
                    elif k == "SD":
                        configs.add((f"{key}.SD", v))
            continue
        if key in ("CLKMUX", "CEMUX", "LSRMUX"):
            for grp in parts[1:]:
                for field in re.split(r"[,:]", grp):
                    if "=" not in field:
                        continue
                    k, v = field.split("=", 1)
                    configs.add((f"{key}.{k}", v.lstrip("#")))
    return props, configs, functions


def env_for(comps: list[Comp]) -> dict:
    props: dict[tuple[str, str], str] = {}
    configs: dict[str, set[tuple[str, str]]] = {}
    sites: set[str] = set()
    for comp in comps:
        site = slice_site(comp.site)
        if site is None:
            continue
        sites.add(site)
        p, c, _f = parse_slice_program(comp.program)
        for k, v in p.items():
            props[(site, k)] = v
        configs.setdefault(site, set()).update(c)
    return {"props": props, "configs": configs, "sites": sites}


def config_equations(tile: BfdTile) -> list[tuple[str, object, list]]:
    out = []
    for name, expr in tile.equations.items():
        if "XPFU" not in name:
            continue
        if "arcval" in expr or ("property(" not in expr and "config(" not in expr):
            continue
        out.append((name, parse_cached(expr), tile.fuse_positions(name)))
    return out


def evaluate_config(tile: BfdTile, comps: list[Comp], equations=None) -> dict[str, bool]:
    env = env_for(comps)
    eqs = config_equations(tile) if equations is None else equations
    return {name: evaluate(ast, env) for name, ast, _ in eqs}


def slice_bits(tile: BfdTile, comps: list[Comp], tile_row: int, col_base: int,
               equations=None) -> list[tuple[int, int, bool]]:
    eqs = config_equations(tile) if equations is None else equations
    values = evaluate_config(tile, comps, eqs)
    out = []
    for name, _ast, positions in eqs:
        for (ram_row, ram_col) in positions:
            out.append((29 * (tile_row - 1) + ram_row, col_base - ram_col,
                        values[name]))
    return out
