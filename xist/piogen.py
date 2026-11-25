"""Synthesise edge PIO programs for nextpnr's pad cells.

`xist/apply.py` evaluates the BFD PIO configuration from an XPN component
program.  A routed JSON has no such program, so this module builds one from the
pad's direction with the vendor's default I/O standard (measured from vendor
runs of the counter design):

=========  ==============================================================
input      ``PADDI:PADDI IOBUF PULLMODE=DOWN IO_TYPE=LVCMOS25 CLAMP=ON``
           ``HYSTERESIS=SMALL ... PGMUX:INBUF INRDMUX:PGMUX``
output     ``TRIMUX:PADDT IOBUF PULLMODE=NONE DRIVE=8 SLEWRATE=SLOW``
           ``CLAMP=OFF ... DATAMUX:PADDO ODMUX:TRIMUX LVDSMUX:DATAMUX``
=========  ==============================================================

The pad site (``PT19A``, ``PB19A``, ``PL12C`` ...) is reconstructed from the
PIO bel position: row 0 is the top edge, row 39 the bottom, column 0 left,
column 50 right.  That string is what ``pioconfig.env_for`` expects.

Outputs use ``DATAMUX:PADDO`` (fabric -> pad directly).  The vendor sometimes
selects ``IOLDO`` because it moves a register into the I/O cell; that only
changes fuses gated by ``IOLOGIC`` modes, which this uarch does not have.
"""

from __future__ import annotations

import re

from .xpn import Comp

PIO_IN = ("PADDI:PADDI "
          "IOBUF:::PULLMODE=DOWN,IO_TYPE=LVCMOS25,DRIVE=NA,SLEWRATE=NA,"
          "CLAMP=ON,OPENDRAIN=OFF,DIFFRESISTOR=OFF,DIFFDRIVE=NA,"
          "BANK_VCCIO=2.5,HYSTERESIS=SMALL,PERSISTENT=OFF "
          "VREF:OFF PGMUX:INBUF INRDMUX:PGMUX")
PIO_OUT = ("TRIMUX:PADDT:::PADDT=0 "
           "IOBUF:::PULLMODE=NONE,IO_TYPE=LVCMOS25,DRIVE=8,SLEWRATE=SLOW,"
           "CLAMP=OFF,OPENDRAIN=OFF,DIFFRESISTOR=OFF,DIFFDRIVE=NA,"
           "BANK_VCCIO=2.5,HYSTERESIS=NA,PERSISTENT=OFF "
           "DATAMUX:PADDO:::PADDO=#SIG VREF:OFF ODMUX:TRIMUX LVDSMUX:DATAMUX")

# Output register packed into the pad's IOLOGIC block (vendor program, measured
# from a design whose only register drives a pad).  The vendor calls it
# ``IREG_OREG`` even though it is an output register.
IOLOGIC_OREG = ("GSR:ENABLED "
                "OUTREG:::REGSET=RESET,GSR=ENABLED,SRMODE=ASYNC,OUTREGMODE=FF "
                "IOLDO:IOLDO MODE:IREG_OREG CEOMUX:1 LSROMUX:0 "
                "CLKOMUX:CLK:::0=0,CLK=#SIG OPOSMUX:OPOS")
IOLOGIC_OREG_RST = ("GSR:ENABLED "
                    "OUTREG:::REGSET=RESET,GSR=ENABLED,SRMODE=LSR_OVER_CE,"
                    "OUTREGMODE=FF "
                    "IOLDO:IOLDO MODE:IREG_OREG CEOMUX:1 LSROMUX:LSRMUX "
                    "CLKOMUX:CLK:::0=0,CLK=#SIG OPOSMUX:OPOS "
                    "LSRMUX:LSR:::LSR=#SIG")

PIO_OUT_IOLDO = PIO_OUT.replace("DATAMUX:PADDO", "DATAMUX:IOLDO")

_XY = re.compile(r"X(\d+)Y(\d+)")
_IO = re.compile(r"PIO_([A-D])$")
ROWS, COLS = 40, 51


def pad_site(col: int, row: int, slot: str) -> str | None:
    """``(19, 39, 'A')`` -> ``PB19A``."""
    if row == 0:
        return f"PT{col}{slot}"
    if row == ROWS - 1:
        return f"PB{col}{slot}"
    if col == 0:
        return f"PL{row}{slot}"
    if col == COLS - 1:
        return f"PR{row}{slot}"
    return None


def _is_output(cell: dict) -> bool:
    dirs = cell.get("port_directions", {})
    if dirs:
        return dirs.get("PAD") == "output"
    return "PADDO" in cell.get("connections", {})


def iologic_comps(routed: dict) -> list[Comp]:
    """Comps for the IOLOGIC blocks of pads that host a packed register."""
    module = None
    for m in routed["modules"].values():
        if "top" in m.get("attributes", {}):
            module = m
            break
    if module is None:
        module = next(iter(routed["modules"].values()))

    out: list[Comp] = []
    reg_by_net: dict[int, dict] = {}
    for cell in module["cells"].values():
        if cell["type"] != "DFF" or "PAD_REG" not in cell.get("parameters", {}):
            continue
        for port, net in cell.get("connections", {}).items():
            if port == "Q" and net:
                reg_by_net[net[0]] = cell

    for cell in module["cells"].values():
        if cell["type"] not in ("PIO", "IBUF", "OBUF"):
            continue
        ioldo = cell.get("connections", {}).get("IOLDO")
        if not ioldo or ioldo[0] not in reg_by_net:
            continue
        reg = reg_by_net[ioldo[0]]
        bel = cell.get("attributes", {}).get("NEXTPNR_BEL", "")
        m = _XY.match(bel)
        mi = _IO.search(bel)
        if not m or not mi:
            continue
        site = pad_site(int(m.group(1)), int(m.group(2)), mi.group(1))
        if site is None:
            continue
        has_reset = "R" in reg.get("connections", {}) or \
                "S" in reg.get("connections", {})
        out.append(Comp(name=f"{site}_MGIOL", site=f"IOL_{site[1:]}",
                        program=IOLOGIC_OREG_RST if has_reset else IOLOGIC_OREG))
    return out


def pio_comps(routed: dict) -> list[Comp]:
    """One Comp per placed PIO cell, with a synthesised program."""
    module = None
    for m in routed["modules"].values():
        if "top" in m.get("attributes", {}):
            module = m
            break
    if module is None:
        module = next(iter(routed["modules"].values()))

    comps = []
    for cell in module["cells"].values():
        if cell["type"] not in ("PIO", "IBUF", "OBUF"):
            continue
        bel = cell.get("attributes", {}).get("NEXTPNR_BEL", "")
        m = _XY.match(bel)
        mi = _IO.search(bel)
        if not m or not mi:
            continue
        col, row, slot = int(m.group(1)), int(m.group(2)), mi.group(1)
        site = pad_site(col, row, slot)
        if site is None:
            continue
        program = PIO_OUT if _is_output(cell) else PIO_IN
        if _is_output(cell) and "IOLDO" in cell.get("connections", {}):
            # registered output: the pad takes its data from the IOLOGIC
            program = PIO_OUT_IOLDO
        comps.append(Comp(name=cell.get("attributes", {}).get("src", "") or site,
                          site=site, program=program))
    return comps
