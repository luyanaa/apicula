"""Convert a Yosys `synth_gowin` JSON netlist into the minimal XIST cell set.

This is a bring-up helper, not a production packer: it renames Gowin LUTn/DFF
cells to the XIST `LUT4`/`DFF`/`PIO` bels, expands LUTn INITs to 16 bits and
drops reset/clock-enable pins (the first uarch has no DFF configuration yet).

Usage:
    python -m xist.nextpnr_json input.json output.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

LUT_BITS = {"LUT1": 2, "LUT2": 4, "LUT3": 8, "LUT4": 16}
DFF_TYPES = {"DFF", "DFFR", "DFFS", "DFFE", "DFFRE", "DFFSE",
             "DFFN", "DFFNR", "DFFNS", "DFFNE", "DFFNRE", "DFFNSE"}


def _expand_lut(init: str, nbits: int) -> str:
    """Expand a 2^n truth table to 16 bits by repeating it for unused inputs."""
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
            dirs = {f"I[{k}]": "input" for k in range(4)
                    if f"I[{k}]" in new_conn}
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
            dirs = {"D": "input", "CLK": "input", "Q": "output"}
            cells[name] = {"type": "DFF", "parameters": {},
                           "attributes": attrs, "connections": new_conn,
                           "port_directions": dirs}
        elif typ == "IBUF":
            cells[name] = {"type": "PIO", "parameters": {}, "attributes": attrs,
                           "connections": {"PAD": conn["I"], "O": conn["O"]},
                           "port_directions": {"PAD": "input", "O": "output"}}
        elif typ == "OBUF":
            cells[name] = {"type": "PIO", "parameters": {}, "attributes": attrs,
                           "connections": {"I": conn["I"], "PAD": conn["O"]},
                           "port_directions": {"I": "input", "PAD": "output"}}
        elif typ in ("VCC", "GND") or typ.startswith("$specify"):
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
