"""Device list and ``'udev.report`` parsing for XiST HqFpga devices."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

# Default install layout used by the research setup.
DEFAULT_DEVICE_DIR = Path("/home/yanlu/Downloads/hqfpga/common/device")


@dataclass
class Part:
    name: str
    die: str
    package: str = ""
    speed: str = ""
    condition: str = ""
    family: str = ""


@dataclass
class Family:
    name: str
    dir: str
    dies: dict[str, dict] = field(default_factory=dict)
    parts: list[Part] = field(default_factory=list)


def load_dv_list(path: str | Path | None = None) -> dict[str, Family]:
    path = Path(path) if path else DEFAULT_DEVICE_DIR / "dv_list.xml"
    root = ET.parse(path).getroot()
    out: dict[str, Family] = {}
    for fam in root.iter("family"):
        name = fam.get("name", "")
        f = Family(name=name, dir=fam.get("dir", ""))
        out[name] = f
        for die in fam.findall("die"):
            dname = die.get("name", "")
            pkgs = [p.text.strip() for p in die.findall("packages/pkg") if p.text]
            speeds = [s.text.strip() for s in die.findall("speedgrades/speed") if s.text]
            conds = [c.text.strip() for c in die.findall("conditions/cond") if c.text]
            f.dies[dname] = {"packages": pkgs, "speeds": speeds, "conditions": conds}
        for p in fam.findall("order_parts/part"):
            f.parts.append(Part(
                name=p.get("name", ""), die=p.get("die", ""),
                package=p.get("pkg", ""), speed=p.get("spd", ""),
                condition=p.get("cond", ""), family=name))
        if not f.parts:
            # synthesise part names when the XML only lists dies
            for dname, info in f.dies.items():
                for pkg in info["packages"]:
                    for spd in info["speeds"] or [""]:
                        for cond in info["conditions"] or [""]:
                            f.parts.append(Part(
                                name=f"{dname}-{spd}{pkg}{cond}", die=dname,
                                package=pkg, speed=spd, condition=cond,
                                family=name))
    return out


def family_for(part: str, path: str | Path | None = None) -> str:
    families = load_dv_list(path)
    for fam in families.values():
        for p in fam.parts:
            if p.name == part:
                return fam.name
    # fall back to prefix heuristics
    prefix = part.split("-")[0]
    for fam in families.values():
        if any(die.startswith(prefix) for die in fam.dies):
            return fam.name
    raise KeyError(f"unknown device {part!r}")


@dataclass
class DeviceReport:
    rows: int
    cols: int
    tiles: list[list[str]]
    sites: list[list[str]]
    counts: dict[str, int]

    def tile_type_counts(self) -> dict[str, int]:
        c: dict[str, int] = {}
        for row in self.tiles:
            for t in row:
                if t and t != ".":
                    c[t] = c.get(t, 0) + 1
        return c

    def tiles_of(self, prefix: str) -> list[tuple[int, int]]:
        out = []
        for r, row in enumerate(self.tiles):
            for c, t in enumerate(row):
                if t.startswith(prefix):
                    out.append((r, c))
        return out


_GRID_RE = re.compile(r"\[V\]:\s*Grid:\s*\((\d+),\s*(\d+)\)")


def parse_udev_report(text: str) -> DeviceReport:
    """Parse the stdout of ``'udev.report``.

    The report contains two grids of identical shape: tile types (PLC/PIC/EBR/...)
    followed by site names (T7/L2/D0/...).  Rows are tab separated and prefixed by
    their row index.
    """
    lines = text.splitlines()
    grids: list[list[list[str]]] = []
    i = 0
    rows = cols = 0
    while i < len(lines):
        m = _GRID_RE.search(lines[i])
        if not m:
            i += 1
            continue
        rows, cols = int(m.group(1)), int(m.group(2))
        i += 2  # skip the column-index header line
        grid: list[list[str]] = []
        while i < len(lines) and len(grid) < rows and re.match(r"^\d+\t", lines[i]):
            cells = lines[i].split("\t")[1:]
            cells = (cells + [""] * cols)[:cols]
            grid.append([c.strip() for c in cells])
            i += 1
        grids.append(grid)
        # second grid (site names) has no header line
        if i < len(lines) and re.match(r"^\d+\t", lines[i]):
            grid2: list[list[str]] = []
            while i < len(lines) and len(grid2) < rows and re.match(r"^\d+\t", lines[i]):
                cells = lines[i].split("\t")[1:]
                cells = (cells + [""] * cols)[:cols]
                grid2.append([c.strip() for c in cells])
                i += 1
            grids.append(grid2)
    if not grids:
        raise ValueError("no '[V]: Grid:' block found in udev report")
    counts = {m.group(1): int(m.group(2))
              for m in re.finditer(r"Count (\S+) : (\d+)", text)}
    return DeviceReport(rows, cols, grids[0],
                        grids[1] if len(grids) > 1 else [],
                        counts)


def parse_pkg_report(text: str) -> list[dict]:
    """Parse ``'udev.pkg.report`` lines like
    ``IO = A4, site = ..., tile = ..., bank = 1, desc = ...``."""
    out = []
    pat = re.compile(
        r"IO = (.*?), site = (.*?), tile = (.*?), bank = (.*?), desc =\s*(.*)$")
    for line in text.splitlines():
        m = pat.search(line.strip())
        if m:
            out.append(dict(zip(("io", "site", "tile", "bank", "desc"),
                                (g.strip() for g in m.groups()))))
    return out
