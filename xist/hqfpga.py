"""Tiny runner for the HqFpga (XiST) command line tool.

HqFpga is a Tcl shell: ``hqfpga -cmd script.tcl`` executes a script in batch
mode.  This module only wraps process invocation and the standard
analyse -> synth -> map -> pack -> place -> route -> bitgen sequence.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

DEFAULT_HQFPGA = "/home/yanlu/Downloads/hqfpga/linux26_x86_64/bin/hqfpga"


def find_hqfpga() -> str:
    for cand in (os.environ.get("HQFPGA"), DEFAULT_HQFPGA):
        if cand and Path(cand).is_file():
            return cand
    found = shutil.which("hqfpga")
    if found:
        return found
    raise FileNotFoundError(
        "hqfpga not found; set $HQFPGA to the binary or install the toolchain"
    )


def run_script(script: str, workdir: str, log: str = "xist.log",
               timeout: int = 900) -> subprocess.CompletedProcess:
    """Run a Tcl script in *workdir*; returns the completed process."""
    exe = find_hqfpga()
    workdir = str(Path(workdir).resolve())
    tcl = Path(workdir) / "_xist_run.tcl"
    tcl.write_text(script, encoding="utf-8")
    proc = subprocess.run(
        [exe, "-cmd", str(tcl), "-log", log],
        cwd=workdir,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return proc


def flow_script(part: str, sources: list[str], top: str,
                verilog_out: str | None = None, bitfile: str | None = None,
                loc: str | None = None, extra_tcl: str = "",
                analysis: str = "design.analyze") -> str:
    """Build the standard implementation script.

    ``part`` must be the full part name (Die-SpeedPackage-Condition), e.g.
    ``SL2-12E-8F256C``; the family is derived by :func:`xist.device.family_for`.
    """
    from .device import family_for

    fam = family_for(part)
    bitgen = {
        "SEALION": "impl.bitgen.xist.sealion",
        "SEAL": "impl.bitgen.xist.seal",
        "SHARK": "impl.bitgen.xist.shark",
    }.get(fam, "impl.bitgen.xist.sealion")
    src = " ".join("{" + s + "}" for s in sources)
    lines = [f"dv.setup {fam} {part}", f"{analysis} {src}",
             f"design.rtlsyn -top {top}", "design.map", "design.pack"]
    if loc:
        lines += ["phycst.start", f"phycst.loc.set SLICE_0 {loc}", "phycst.end"]
    lines += ["design.place -seed 1", "design.route"]
    if verilog_out:
        lines.append(f"nl.write {verilog_out} -phy -loc")
    if bitfile:
        lines.append(f"{bitgen} {bitfile}")
    if extra_tcl:
        lines.append(extra_tcl)
    lines.append("exit")
    return "\n".join(lines) + "\n"
