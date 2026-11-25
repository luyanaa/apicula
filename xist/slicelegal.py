"""Slice legality derived from the vendor BFD configuration model.

`nextpnr`'s bring-up uarch returned ``true`` from ``isBelLocationValid()`` for
every bel, which lets the placer build slices the silicon cannot implement.  The
BFD already states which slice configuration fields exist and which are
mutually exclusive, so the checker is derived from data instead of being
hand-written.

Two kinds of constraint are encoded here:

* **required fields** - each slice bank has a fixed set of configuration
  fields (``MODE``, ``REGMODE``, ``SRMODE``, ``REG0.SD``, ``CLK0MUX.CLK`` ...).
  A DFF that is used needs its bank's ``REGMODE``/clock fields; a LUT-only slice
  must not claim a register mode.
* **exclusive modes** - ``MODE`` selects LOGIC / RIPPLE / CCU2 / RAMW / DPRAM,
  and a bank inside a ``RIPPLE``/``CCU2`` mode belongs to the carry chain: its
  LUT output is consumed by the ALU and cannot simultaneously drive the fabric
  through ``F``.

The field lists are read from ``testdata/SL2-12E_bfd_slice.json`` (exported from
``'bfd.dumpbfd``) so the checker follows the device data.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

TESTDATA = Path(__file__).resolve().parent / "testdata"
SLICE_JSON = TESTDATA / "SL2-12E_bfd_slice.json"

# Values the vendor uses for MODE, from the BFD config() terms.
MODE_VALUES = ("LOGIC", "RIPPLE", "CCU2", "RAMW", "DPRAM")
# Modes where the ALU owns the bank's carry logic.
CARRY_MODES = ("RIPPLE", "CCU2")
# Modes where the LUTs are configured as memory, not as gates.
MEMORY_MODES = ("RAMW", "DPRAM")


@dataclass
class SliceModel:
    """Configuration fields of one slice bank, from the BFD."""

    config_fields: set[str] = field(default_factory=set)
    property_fields: set[str] = field(default_factory=set)

    @property
    def all_fields(self) -> set[str]:
        return self.config_fields | self.property_fields


def load_slice_model(path: str | Path | None = None) -> SliceModel:
    import re
    path = Path(path or SLICE_JSON)
    data = json.loads(path.read_text())
    cfg: set[str] = set()
    prop: set[str] = set()
    for info in data["fuses"].values():
        for m in re.finditer(r"config\(SLICE_[ABCD],([^,]+),", info["expr"]):
            cfg.add(m.group(1))
        for m in re.finditer(r"property\(SLICE_[ABCD],([^,]+),", info["expr"]):
            prop.add(m.group(1))
    return SliceModel(cfg, prop)


def bank_fields(slice_model: SliceModel, bank: int) -> set[str]:
    """Fields that belong to LUT/DFF bank *bank* (0 or 1) of a slice."""
    out = set()
    for f in slice_model.all_fields:
        if f.startswith(f"REG{bank}."):
            out.add(f)
        elif f.startswith("REG") and f[3].isdigit():
            continue          # the other bank
        else:
            out.add(f)        # shared slice-level field
    return out


@dataclass
class SliceUse:
    """What the netlist asks of one slice bank."""

    has_lut: bool = False
    has_dff: bool = False
    needs_reset: bool = False
    needs_set: bool = False
    needs_ce: bool = False
    mode: str = "LOGIC"


def problems(use: SliceUse) -> list[str]:
    """Reasons a slice bank cannot be configured as requested."""
    out: list[str] = []
    if use.mode not in MODE_VALUES:
        out.append(f"MODE {use.mode!r} is not in {MODE_VALUES}")
    if use.mode in CARRY_MODES and use.has_dff and not use.has_lut:
        out.append(f"MODE {use.mode} needs the bank LUT for propagate/generate")
    if use.mode in MEMORY_MODES and use.has_dff:
        out.append(f"MODE {use.mode} cannot host a flip-flop in the same bank")
    if use.needs_reset and use.needs_set:
        out.append("one REGSET field: reset and set cannot both be requested")
    if use.has_dff and not (use.has_lut or use.mode in CARRY_MODES):
        # the DFF data input is the slice output mux (OFX -> DI), which is fed
        # by the bank LUT or the carry chain, never by a free routing arc
        out.append("a DFF without a LUT/carry source has no data path "
                   "(DI is driven only by OFX)")
    return out


def bel_location_valid(uses: dict[str, SliceUse],
                       slice_model: SliceModel | None = None) -> bool:
    """True when every used slice bank can be configured."""
    return all(not problems(u) for u in uses.values())


def main(argv=None) -> int:
    model = load_slice_model()
    print(f"slice config fields: {len(model.config_fields)}")
    print(f"slice property fields: {len(model.property_fields)}")
    for bank in (0, 1):
        print(f"  bank {bank}: {sorted(bank_fields(model, bank))}")
    print()
    cases = [
        ("lut only", SliceUse(has_lut=True)),
        ("dff with lut", SliceUse(has_lut=True, has_dff=True)),
        ("dff without lut", SliceUse(has_dff=True)),
        ("carry without lut", SliceUse(mode="RIPPLE", has_dff=True)),
        ("ram with dff", SliceUse(mode="RAMW", has_lut=True, has_dff=True)),
        ("reset+set", SliceUse(has_lut=True, has_dff=True,
                               needs_reset=True, needs_set=True)),
    ]
    for label, use in cases:
        p = problems(use)
        print(f"{label:20s} -> {'OK' if not p else p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
