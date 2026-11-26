"""Clock-network enable fuses on the spine tiles.

The 2-column spine tile types (`CENTER6_12K`, `CENTER7_12K`, `CENTER8_12K`,
`CENTERA_12K`, ...) carry the device clock tree: their fuses are pure
`arcval(source, SNET_n)` muxes such as

    MC1_SNET1_I_0 = arcval(SNETCIBT_1,   SNET_1)
    MC1_SNET2_I_1 = arcval(SNETCIBMID_4, SNET_2)

``SNET_*`` never appears in an XPN, so `used_arcs()` can never derive them.
They are, however, decidable from the *shape* of the design, which is what this
module expresses.  Measured against five vendor bitstreams, per spine bit:

===================  ==========================================  ============
bit                  meaning (inferred from 5 designs)          rule
===================  ==========================================  ============
(517, 1)             a clock network is in use                 has_clock
(514, 1)             a clock network is in use                 has_clock
(225, 1)             IOLOGIC input register/IDDR used          not derivable
(689, 1)             counter only                              not derivable
(892, 1)             counter + oreg                            not derivable
===================  ==========================================  ============

Measured table (counter / oreg / ireg / oddr / empty):

    (517,1)  1 1 1 1 0      (689,1)  1 0 0 0 0
    (514,1)  1 1 1 1 0      (892,1)  1 1 0 0 0
    (225,1)  0 0 1 1 0      <- only designs whose *data* pad uses IOLOGIC

The remaining three depend on which specific ``SNET`` mux the design's clock and
pad paths reach; the routed JSON carries the *net*, not the tile-local clock
spine track, so they stay at the template value and are reported by the writer.
"""

from __future__ import annotations


def spine_bits(has_clock: bool, has_input_pad: bool) -> dict[tuple[int, int], bool]:
    """Absolute ``(bit_row, bit_col)`` values derivable from the design shape."""
    return {
        (517, 1): has_clock,        # CENTER6_12K row 18, SNET1_I_0
        (514, 1): has_clock,        # CENTER6_12K row 18, SNET2_I_1
    }


def design_shape(routed: dict) -> tuple[bool, bool]:
    """``(has_clock, has_input_pad)`` from a nextpnr routed JSON."""
    module = None
    for m in routed["modules"].values():
        if "top" in m.get("attributes", {}):
            module = m
            break
    if module is None:
        module = next(iter(routed["modules"].values()))

    has_clock = False
    has_input_pad = False
    for cell in module["cells"].values():
        ports = cell.get("connections", {})
        # any sequential cell (slice DFF, IOLOGIC-register DFF, ODDR/IDDR)
        # means the clock tree is live
        if "CLK" in ports and cell["type"] in ("DFF", "ODDR", "IDDR"):
            has_clock = True
        if cell["type"] in ("PIO", "IBUF") and \
                (ports.get("PADDI") or ports.get("IOLTO")):
            has_input_pad = True
    return has_clock, has_input_pad
