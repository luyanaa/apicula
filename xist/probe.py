"""Differential probes against the vendor P&R engine.

The vendor flow is deterministic with ``design.place -seed 1`` and lets us pin a
SLICE with ``phycst.loc.set``.  That makes it possible to learn the bitstream by
generating pairs of designs that differ in exactly one feature and diffing the
resulting .bit files (the same idea as Apicula's Gowin fuzzers).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from . import hqfpga
from .bitstream import Bitstream, diff_positions


@dataclass
class ProbeResult:
    name: str
    workdir: Path
    bit: Bitstream
    phy_verilog: str | None
    stdout: str
    returncode: int


def lut_design(inits: list[int], inputs: list[str] | None = None) -> str:
    """Verilog with N bare xsLUT4 primitives (defparam init)."""
    n = len(inits)
    inputs = inputs or ["a", "b", "c", "d"]
    ports = ", ".join(f"o{i}" for i in range(n))
    body = []
    for k in range(n):
        body.append(f"  xsLUT4 u{k} (.A({inputs[0]}),.B({inputs[1]}),"
                    f".C({inputs[2]}),.D({inputs[3]}),.Z(o{k}));")
    for k, init in enumerate(inits):
        body.append(f"  defparam u{k}.init = 16'h{init:04x};")
    return (f"module t(input {', '.join(inputs)}, output {ports});\n"
            + "\n".join(body) + "\nendmodule\n")


def run_lut_probe(part: str, workdir: str | Path, inits: list[int], name: str,
                  loc: str | None = None, timeout: int = 900) -> ProbeResult:
    """Synthesise/place/route a one-slice LUT design and return the bitstream."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    src = f"{name}.v"
    (workdir / src).write_text(lut_design(inits), encoding="utf-8")
    script = hqfpga.flow_script(
        part, [src], "t", verilog_out=f"{name}_phy.v",
        bitfile=f"{name}.bit", loc=loc, xpn_out=f"{name}.xpn")
    proc = hqfpga.run_script(script, str(workdir), log=f"{name}.log", timeout=timeout)
    bit_path = workdir / f"{name}.bit"
    bs = Bitstream.read(str(bit_path)) if bit_path.is_file() else None
    phy = (workdir / f"{name}_phy.v").read_text(encoding="utf-8") \
        if (workdir / f"{name}_phy.v").is_file() else None
    return ProbeResult(name, workdir, bs, phy, proc.stdout, proc.returncode)


_PHY_LUT_RE = re.compile(
    r"xsLUT4\s+(\S+)\s*(?:/\* LOC : (\S+) \*/)?\s*\(([^;]*?)\);\s*"
    r"defparam\s+\S+\.init\s*=\s*16'H([0-9a-fA-F]+)", re.S)


def parse_phy_luts(verilog: str) -> list[dict]:
    """Return [{name, loc, ports, init}] from a placed netlist."""
    out = []
    for m in _PHY_LUT_RE.finditer(verilog):
        ports = {}
        for pm in re.finditer(r"\.(\w+)\(([^)]*)\)", m.group(3)):
            ports[pm.group(1)] = pm.group(2).strip()
        out.append(dict(name=m.group(1), loc=m.group(2),
                        ports=ports, init=int(m.group(4), 16)))
    return out


def diff_probes(a: ProbeResult, b: ProbeResult,
                row_range: tuple[int, int] | None = None):
    if a.bit is None or b.bit is None:
        raise RuntimeError(f"probe missing bitstream: {a.name} / {b.name}")
    return diff_positions(a.bit, b.bit, row_range)


_PORT_RE = re.compile(r"xsLUT4\s+\S+[^;]*?\.Z\((\w+)\)", re.S)


def lut_port(phy_verilog: str | None) -> str | None:
    """Return the slice output port (F0/F1) driven by the first LUT."""
    if not phy_verilog:
        return None
    # only look inside the slice wrapper: take the first xsLUT4 instance
    m = _PORT_RE.search(phy_verilog)
    return m.group(1) if m else None
