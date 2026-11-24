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
* The graph is a bring-up model: a 32-wire per-tile CIB crossbar plus a
  complete 1-row long-wire mesh, no vendor XPN needed.  It is not the vendor
  fuse map yet; the bitstream writer still needs the real pip -> bits mapping.

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
* Routing fuse map (pip -> bits): 8 candidate pips validated on a holdout set,
  in `testdata/SL2-12E_pip_fuses_sample.json`; `xist/program.py` writes them
  into a `.bit`.  The full map still needs a larger routing-only campaign.
  The long-wire naming (`V06S0300` -> `V06S0302` -> ...) is decoded and the
  intermediate wires are generated, but the CIB/track-switch graph is still
  incomplete (a one-LUT IO design does not route yet).
* `.bit` checksum and `.bin` compression.
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
