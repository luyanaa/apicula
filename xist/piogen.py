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
# Input pad whose data feeds the pad's IOLOGIC input register/IDDR: the vendor
# keeps PADDI but selects the IOLOGIC path through TRIMUX/DATAMUX below.
PIO_IN_IOLTO = PIO_IN.replace("PGMUX:INBUF", "TRIMUX:PADDT PGMUX:INBUF")

# IOLOGIC input register (vendor: IOL_<pad> with FF/IN, MODE IREG_OREG).
IOLOGIC_IREG = ("GSR:ENABLED FF:::REGSET=RESET,INREGMODE=FF,GSR=ENABLED,"
                "SRMODE=ASYNC IN:IN MODE:IREG_OREG "
                "CLKIMUX:CLK:::0=0,CLK=#SIG LSRIMUX:0")
IOLOGIC_IREG_RST = ("GSR:ENABLED FF:::REGSET=RESET,INREGMODE=FF,GSR=ENABLED,"
                    "SRMODE=ASYNC IN:IN MODE:IREG_OREG "
                    "CLKIMUX:CLK:::0=0,CLK=#SIG LSRIMUX:LSRMUX "
                    "LSRMUX:LSR:::LSR=#SIG")

# DDR primitives, measured from xsODDR/xsIDDRX1/xsODDRX2 vendor runs.
IOLOGIC_ODDR = ("GSR:ENABLED IOLDO:IOLDO MODE:IDDR_ODDR LSROMUX:0 "
                "CLKOMUX:CLK:::0=0,CLK=#SIG OPOSMUX:OPOS "
                "ODDR:::GSR=ENABLED ONEGMUX:ONEG")
IOLOGIC_IDDR = ("GSR:ENABLED IN:IN MODE:IDDR_ODDR "
                "CLKIMUX:CLK:::0=0,CLK=#SIG LSRIMUX:0 "
                "IDDR:::GSR=ENABLED IP:IP")
IOLOGIC_ODDR2 = ("GSR:ENABLED IOLDO:IOLDO MODE:ODDR2 LSROMUX:0 "
                 "CLKOMUX:CLK:::0=0,CLK=#SIG "
                 "ODDR2:::GSR=ENABLED TXD0MUX:TXD0 TXD1MUX:TXD1 "
                 "TXD2MUX:TXD2 TXD3MUX:TXD3")

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


def _ddr_program(cell: dict) -> str:
    kind = cell["type"]
    if kind == "IDDR":
        return IOLOGIC_IDDR
    ports = cell.get("connections", {})
    if kind == "ODDR2" or "D3" in ports:
        return IOLOGIC_ODDR2
    return IOLOGIC_ODDR


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
        params = cell.get("parameters", {})
        if "PAD_REG" not in params and "PAD_IREG" not in params and \
                "PAD_DDR" not in params:
            continue
        for port, net in cell.get("connections", {}).items():
            if port in ("Q", "Q0") and net:
                reg_by_net[net[0]] = cell

    for cell in module["cells"].values():
        if cell["type"] not in ("PIO", "IBUF", "OBUF"):
            continue
        ports = cell.get("connections", {})
        ioldo = ports.get("IOLDO")
        iolto = ports.get("IOLTO")
        reg = None
        if ioldo and ioldo[0] in reg_by_net:
            reg = reg_by_net[ioldo[0]]
        elif iolto and iolto[0] in reg_by_net:
            reg = reg_by_net[iolto[0]]
        if reg is None:
            continue
        bel = cell.get("attributes", {}).get("NEXTPNR_BEL", "")
        m = _XY.match(bel)
        mi = _IO.search(bel)
        if not m or not mi:
            continue
        site = pad_site(int(m.group(1)), int(m.group(2)), mi.group(1))
        if site is None:
            continue
        params = reg.get("parameters", {})
        rports = reg.get("connections", {})
        has_reset = "R" in rports or "S" in rports
        if "PAD_DDR" in params:
            program = _ddr_program(reg)
        elif "PAD_IREG" in params:
            program = IOLOGIC_IREG_RST if has_reset else IOLOGIC_IREG
        else:
            program = IOLOGIC_OREG_RST if has_reset else IOLOGIC_OREG
        out.append(Comp(name=f"{site}_MGIOL", site=f"IOL_{site[1:]}",
                        program=program))
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
        ports = cell.get("connections", {})
        if _is_output(cell) and "IOLDO" in ports:
            # registered output: the pad takes its data from the IOLOGIC
            program = PIO_OUT_IOLDO
        elif not _is_output(cell) and "IOLTO" in ports:
            # input register/IDDR: data is taken from the pad into the IOLOGIC
            program = PIO_IN_IOLTO
        comps.append(Comp(name=cell.get("attributes", {}).get("src", "") or site,
                          site=site, program=program))
    return comps
