# Wiring the XIST database into nextpnr

The `.bba` produced by `xist.chipdb` is the same format as nextpnr-himbaechel's
Gowin chipdb.  A minimal `xist` uarch now lives in
[`nextpnr/uarch/xist`](nextpnr/uarch/xist); see
[`nextpnr/README.md`](nextpnr/README.md) for the apply/build/run recipe.

## Status (nextpnr e4c7859, 2026-11-24)

* uarch compiles with `-DARCH=himbaechel -DHIMBAECHEL_UARCH=xist`.
* chipdb loads (51x40, 2040 tile instances, 43 tile types, 196 pads).
* a 6-cell design (LUT4 + DFF + 4 PIO) **places successfully** with
  `--no-route`; HeAP/SA finish normally and a checksum is printed.
* `--route` **succeeds for arbitrary LUT/DFF/ALU/MUX/IO designs** on the
  bring-up graph: `python -m xist.demo_any design.v` reports `Routing complete`
  and `Program finished normally`.  Verified with 16 designs including adders,
  counters, comparators, multipliers, muxes, LFSRs and pipelines.
* The graph is a bring-up model by default: a 32-wire per-tile CIB crossbar
  plus a complete 1-row long-wire mesh, no vendor XPN needed.
* `build_chip(..., bfd_profiles={"PLC": ...})` installs the vendor BFD routing
  pips for PLC and puts the arc -> fuse positions in the sidecar.
  `xist.demo_any --bfd-replace` routes LUT/IO designs through vendor pips only;
  every PLC pip used was fuse-backed in the measured designs (mux4 29/29,
  io_pass 22/22).

## Database contents

* One tile type per `'udev.report` token (`PLC`, `PIC_*`, `EBR`, `DSP`, ...).
* `PLC`: four slices, each with two `LUT4` and two `DFF` bels.  Wire names use
  the vendor XPN convention (`F0..F7`, `DI0..DI7`, `Q0..Q7`, `A0..D7`), so pips
  parsed from XPN dumps attach directly to the bel pins.
* `PIC`: four `PIO_A..D` bels with `PAD/I/O/OE`.
* Package pins from `'udev.pkg.report` are attached as pads
  (`PL12C` -> X0Y12 / `PIO_C`).
* Routing: every `dst.src` pair in the XPN corpus becomes a pip (same tile) or a
  node (cross-tile long wire).  This is intentionally sparse.

The sidecar JSON (`--out-meta`) carries the bitstream data nextpnr does **not**
store in the bba: the per-slice LUT INIT fuse maps and the column-origin table.
The uarch's bitstream writer will load these when emitting `.bit`.

## Known gaps

* **Per-tile LUT layout**: the LUT minterm permutation varies with tile position
  (4x4 grid samples in `testdata/SL2-12E_lut_grid4.json`).  The block origin is
  known from `SL2-12E_columns.json`; the permutation needs a per-tile campaign
  (`python -m xist.cli tiles ...`) or an equivalence-class study.
* DFF / PIO / EBR / DSP fuse maps.
* Routing fuse map (pip -> bits): **complete for the CIB/long-wire network**
  from the vendor BFD (`'bfd.dumpbfd`): 3315 arcs (1976 long-wire) over 56
  routing tile types in 28 profiles (`testdata/SL2-12E_bfd_profiles.json`).
  That includes the 2-column centre tiles, the 29x48 CIB family and the 29x59
  edge/PIC types; PLC detail is in `testdata/SL2-12E_bfd_PLC.json` (2500 arcs,
  1392 fuses, 1128 routing fuses).
  Bit coordinates come from `xist/bfdmap.py` + `SL2-12E_bitcol_base.json`.
  Validated on 305 PLC tile instances (344040/344040 bits) and on 331
  edge-tile instances at 59-column tile columns 1 and 49 (383294/383298 bits,
  one outlier using a different edge profile).  `xist/program.py`
  (`program_routing`) writes them from an XPN.
  Tile columns 0 and 50 have no bitstream columns of their own (their I/O cells
  are configured through the neighbouring 59-column block), and fuses whose
  equation mixes `config()` with `arcval()` still need the config model.
  The fuzz campaign (`xist/campaign.py`, 231 holdout-validated candidates)
  remains as an independent cross-check: 128/135 of its vendor-named pips are
  predicted exactly by the BFD map.
  The long-wire naming (`V06S0300` -> `V06S0302` -> ...) is decoded and the
  intermediate wires are generated, including the H00L/H00R and V00T/V00B
  boundary pairs.
  BFD-only routing currently works for LUT/IO designs.  DFF-heavy designs need
  LUT/DFF packing: a DFF data input is the slice output mux (`OFX -> DI`,
  configured by slice fuses), not a free routing arc, so standalone DFFs (as
  the bring-up packer emits) cannot route without a synthetic fallback.
  The edge ring is split into contextual tile types and mapped with
  `default_bfd_profiles()`: CTEST row 1/38 -> CIB_CFG, CTEST in the EBR/DSP
  bands -> CIB_EBR, CTEST columns 1/49 -> CIB_EBR0_END0 / CIB_EBR2_END1, EBR ->
  CIB_EBR, D0..D8 -> CIB_DSP, with left/right edge variants.  Routed designs
  now use fuse-backed CTEST/EBR/DSP pips too (e.g. wide_eq: CTEST_CFG 3/3,
  CTEST_EBR 1/1).
  Still synthetic: PIC (column 0), PIC_right (column 50), PIC_top/bot and the
  `-12k` corners.  Their bits live in the neighbouring 59-column block and
  their equations include negated arcs + wire guards
  (`!arcval(...) && wire(...)`).  The parser/export and `program_routing` now
  support that form, but the PIO mapping itself is not settled.  A 87-site
  probe campaign (see doc/xist.md) found that the input-mode bit pattern is
  stable per (side, slot) across rows and reproduces 1283/1289 corpus bits
  (99.53%).  Most of those bits are PIO configuration, not BFD routing arcs; the BFD
  `property()`/`config()` equations are now evaluated directly
  (`xist/bfd_expr.py`, `xist/pioconfig.py`, profiles in
  `testdata/SL2-12E_bfd_pio.json`).  Left and right edge pads match the vendor
  bitstream exactly: 411404/411404 configuration bits over 1789 tile groups,
  both read-only and through `program_pio`.  The sidecar points the writer at
  the profile (`pio_config`).
  Top/bottom pads are the 6-row PIC_T5/PIC_B5 profiles at row base 1108/1102
  (column 7/45 use a -2/-1 base correction).  All 3353 edge tile groups match
  322991/322991 bits (100%), read-only and through `program_pio`.
  `xist.apply` applies the PIO configuration to a `.bit` template from an XPN
  (`--check` compares with a vendor bitstream; c0: 863/863 bits).
  Remaining: the `-12k` corners are CENTER/CFG/DSP spine tiles carrying
  long-wire routing, not PIO; their 2-column bit mapping is uncalibrated.
* `.bit` checksum: **decoded** (CRC-16/0x8005 over bit-packed rows XOR
  0x1FE2; verified on 371 vendor bitstreams).  `.bin` compression is still
  unknown.
* Timing model.
* Real packer (LUT/DFF pairing, constants, IO attributes); the bring-up uarch
  only removes nextpnr's synthetic IOBs.

## Next milestones

1. Routing golden corpus: generate designs that exercise every wire type, dump
   XPN, and build a connected graph (bel pins already use the vendor names).
2. Per-tile LUT calibration or an equivalence-class table.
3. Real packer + bitstream writer for LUT/DFF/PIO on SL2-12E.
4. Yosys `synth_xist` derived from `synth_gowin`.
5. Timing sampling from `ta.report` and PVT fit.
