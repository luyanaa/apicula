"""Convert a Yosys `synth_gowin` JSON netlist into the XIST cell set.

Renames Gowin LUTn/DFF cells to the XIST ``LUT4``/``DFF``/``PIO`` bels and
expands LUTn INITs to 16 bits.

DFF semantics are preserved as *configuration*, matching the vendor slice
program (``REG0:::REGSET=RESET,CLKDELAY=DEL0:SD=1 ... SRMODE:ASYNC``): the
reset, clock-enable and polarity pins become cell parameters instead of being
dropped, so the bitstream writer can emit ``REG0.REGSET`` / ``CEMUX`` /
``LSRMUX`` / ``REGMODE`` from the netlist:

===================  ==========================================
Gowin cell           XIST DFF parameters
===================  ==========================================
DFF                  (none)
DFFR / DFFNR         RESET=1  (inverted for the N variants)
DFFS / DFFNS         SET=1
DFFE / DFFNE         CE=1
DFFRE / DFFNRE       RESET=1, CE=1
DFFSE / DFFNSE       SET=1, CE=1
===================  ==========================================

``CLK_POLARITY`` / ``RESET_POLARITY`` keep the vendor's sense, and
``SRMODE`` records whether reset/set is asynchronous.

Usage:
    python -m xist.nextpnr_json input.json output.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

LUT_BITS = {"LUT1": 2, "LUT2": 4, "LUT3": 8, "LUT4": 16}
DFF_TYPES = {"DFF", "DFFR", "DFFS", "DFFE", "DFFRE", "DFFSE",
             "DFFN", "DFFNR", "DFFNS", "DFFNE", "DFFNRE", "DFFNSE",
             "DFFC", "DFFCE", "DFFNC", "DFFNCE",
             "DFFP", "DFFPE", "DFFNP", "DFFNPE"}
# Two different reset semantics, per the Gowin simulation models:
#   DFFR/DFFS (port RESET/PRESET) -> sampled on the clock edge  => synchronous
#   DFFC/DFFP (port CLEAR/PRESET) -> combinational path         => asynchronous
DFF_RESET_SYNC = {"DFFR", "DFFRE", "DFFNR", "DFFNRE"}
DFF_RESET_ASYNC = {"DFFC", "DFFCE", "DFFNC", "DFFNCE"}
DFF_SET_SYNC = {"DFFS", "DFFSE", "DFFNS", "DFFNSE"}
DFF_SET_ASYNC = {"DFFP", "DFFPE", "DFFNP", "DFFNPE"}
DFF_RESET = DFF_RESET_SYNC | DFF_RESET_ASYNC
DFF_SET = DFF_SET_SYNC | DFF_SET_ASYNC
DFF_CE = {"DFFE", "DFFRE", "DFFSE", "DFFNE", "DFFNRE", "DFFNSE",
          "DFFCE", "DFFNCE", "DFFPE", "DFFNPE"}


def _expand_lut(init: str, nbits: int) -> str:
    """Expand a 2^n truth table to 16 bits by repeating it for unused inputs.

    Unused LUT inputs are tied low by the packer, so the block is replicated
    over the upper address bits.
    """
    v = int(init, 2) & ((1 << nbits) - 1)
    reps = 16 // nbits
    full = 0
    for k in range(reps):
        full |= v << (k * nbits)
    return format(full, "016b")


def convert(module: dict) -> dict:
    cells = {}
    for name, cell in module.get("cells", {}).items():
        typ = cell["type"]
        conn = dict(cell.get("connections", {}))
        params = dict(cell.get("parameters", {}))
        attrs = dict(cell.get("attributes", {}))
        if typ in LUT_BITS:
            nbits = LUT_BITS[typ]
            init = _expand_lut(params.get("INIT", "0" * nbits), nbits)
            new_conn = {}
            for k in range(4):
                key = f"I{k}"
                if key in conn:
                    new_conn[f"I[{k}]"] = conn[key]
            if "F" in conn:
                new_conn["F"] = conn["F"]
            dirs = {k: "input" for k in new_conn if k.startswith("I[")}
            if "F" in new_conn:
                dirs["F"] = "output"
            cells[name] = {"type": "LUT4",
                           "parameters": {"INIT": init},
                           "attributes": attrs, "connections": new_conn,
                           "port_directions": dirs}
        elif typ in DFF_TYPES:
            new_conn = {}
            for src, dst in (("D", "D"), ("CLK", "CLK"), ("Q", "Q")):
                if src in conn:
                    new_conn[dst] = conn[src]
            params_out: dict = {}
            negative = typ.startswith("DFFN")
            # DFFN* is a negative-edge register, not inverted data
            if negative:
                params_out["CLK_POLARITY"] = "0"
            # reset / set: R/S and the CLEAR/PRESET aliases of DFFC/DFFP
            if typ in DFF_RESET:
                pin = next((p for p in ("CLEAR", "RESET", "R") if p in conn), None)
                if pin is not None:
                    new_conn["R"] = conn[pin]
                    params_out["RESET"] = "1"
                    params_out["RESET_POLARITY"] = "1"
                    params_out["RESET_ASYNC"] = "1" if typ in DFF_RESET_ASYNC else "0"
            if typ in DFF_SET:
                pin = next((p for p in ("PRESET", "S") if p in conn), None)
                if pin is not None:
                    new_conn["S"] = conn[pin]
                    params_out["SET"] = "1"
                    params_out["SET_POLARITY"] = "1"
                    params_out["RESET_ASYNC"] = "1" if typ in DFF_SET_ASYNC else "0"
            if typ in DFF_CE and "CE" in conn:
                new_conn["CE"] = conn["CE"]
                params_out["CE"] = "1"
            dirs = {"D": "input", "CLK": "input", "Q": "output"}
            for pin in ("R", "S", "CE"):
                if pin in new_conn:
                    dirs[pin] = "input"
            cells[name] = {"type": "DFF", "parameters": params_out,
                           "attributes": attrs, "connections": new_conn,
                           "port_directions": dirs}
        elif typ == "IBUF":
            cells[name] = {"type": "PIO", "parameters": {}, "attributes": attrs,
                           "connections": {"PAD": conn["I"], "PADDI": conn["O"]},
                           "port_directions": {"PAD": "input", "PADDI": "output"}}
        elif typ == "OBUF":
            cells[name] = {"type": "PIO", "parameters": {}, "attributes": attrs,
                           "connections": {"PADDO": conn["I"], "PAD": conn["O"]},
                           "port_directions": {"PADDO": "input", "PAD": "output"}}
        elif typ in ("MUX2_LUT5", "MUX2_LUT6", "MUX2_LUT7", "MUX2_LUT8"):
            # O = S0 ? I1 : I0  ->  LUT4 with I[0]=I0, I[1]=I1, I[2]=S0
            init = 0
            for d in (0, 1):
                for sbit in (0, 1):
                    for i1 in (0, 1):
                        for i0 in (0, 1):
                            bit = i1 if sbit else i0
                            init |= bit << (d * 8 + sbit * 4 + i1 * 2 + i0)
            new_conn = {}
            for src, dst in (("I0", "I[0]"), ("I1", "I[1]"), ("S0", "I[2]")):
                if src in conn:
                    new_conn[dst] = conn[src]
            if "O" in conn:
                new_conn["F"] = conn["O"]
            cells[name] = {"type": "LUT4",
                           "parameters": {"INIT": format(init, "016b")},
                           "attributes": attrs, "connections": new_conn,
                           "port_directions": {k: "input" for k in new_conn
                                               if k.startswith("I[")} |
                                              ({"F": "output"} if "F" in new_conn else {})}
        elif typ == "ALU":
            dirs = {"I0": "input", "I1": "input", "I2": "input", "I3": "input",
                    "CIN": "input", "SUM": "output", "COUT": "output"}
            cells[name] = {"type": "ALU", "parameters": params,
                           "attributes": attrs,
                           "connections": {k: v for k, v in conn.items() if k in dirs},
                           "port_directions": {k: dirs[k] for k in conn if k in dirs}}
        elif typ in ("VCC", "GND"):
            # keep them: they are real constant drivers, and a dangling LUT
            # input would otherwise float.  The packer ties them to the tile's
            # constant nets.
            cells[name] = {"type": typ, "parameters": {},
                           "attributes": attrs,
                           "connections": {"Y": conn.get("Y")},
                           "port_directions": {"Y": "output"}}
        elif typ.startswith("$specify"):
            continue
        else:
            raise ValueError(f"unsupported cell {name}: {typ}")
    out = dict(module)
    out["cells"] = cells
    return out


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) < 2:
        print(__doc__)
        return 2
    data = json.loads(Path(argv[0]).read_text())
    modules = data["modules"]
    top = next((k for k, m in modules.items()
                if "top" in m.get("attributes", {})), None)
    if top is None:
        top = "top" if "top" in modules else next(iter(modules))
    modules[top] = convert(modules[top])
    data["modules"] = {top: modules[top]}
    Path(argv[1]).write_text(json.dumps(data, indent=1) + "\n")
    print(f"{argv[1]}: {len(modules[top]['cells'])} cells")
    return 0


if __name__ == "__main__":
    sys.exit(main())
