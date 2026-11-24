# Wiring the XIST database into nextpnr

The `.bba` produced here is the same format as `nextpnr-himbaechel`'s Gowin
chipdb.  The remaining work is a `uarch` implementation; this note records the
integration points and what the database currently provides.

## Expected tree

```
nextpnr/
  himbaechel/
    uarch/
      xist/
        CMakeLists.txt
        constids.inc
        xist.cc            # UArch hooks: pack, validity, bel pins, globals
        xist.h
        pack_lut.cc        # LUT4 + DFF -> SLICE
        pack_io.cc         # PIO / IOLOGIC
        pack_mem.cc        # EBR
        pack_dsp.cc        # MULT18 / ALU
        pcf.cc             # .upc / .cst reader
    chipdb/
      xist-SL2-12E.bin     # produced by  python -m xist.bbasm ...
```

Build (from a nextpnr checkout):

```bash
cmake -B build -DARCH=himbaechel -DHIMBAECHEL_UARCH=xist
cmake --build build -j$(nproc)
nextpnr-himbaechel --device SL2-12E --json top.json --write pnr.json
```

Install the blob under `share/himbaechel/xist/` (or pass it explicitly, depending
on how the uarch looks it up).

## What the database already contains

* 51 x 40 grid (`width=51`, `height=40`) matching `'udev.report`.
* Tile types for every grid token (`PLC`, `PIC`, `PIC_top`, `PIC_bot`,
  `PIC_right`, `EBR`, `DSP`, `CTEST`, hard-IP tokens).
* `PLC`: four slices, each with `LUT0/1` (`LUT4`) and `DFF0/1` (`DFF`) bels and
  slice-local wires `S{n}_{A,B,C,D,F0,F1,Q0,Q1,CLK,LSR,DI0,DI1}`.
* `PIC`: four `PIO` bels with `PAD/I/O/OE`.
* Hard tiles: placeholder `IN/OUT` bels until pin maps are extracted.
* Routing: pips/nodes observed in the XPN corpus (incomplete by construction).

The sidecar JSON (`--out-meta`) carries the bitstream data nextpnr does **not**
store in the bba: the per-slice LUT INIT fuse maps.  The uarch's bitstream writer
should embed those tables (or load the JSON at build time) when emitting `.bit`.

## UArch hooks to implement first

1. **Cell types**: `LUT4` (pins A,B,C,D,Z), `DFF` (D,CK,Q), `PIO` (PAD,I,O,OE).
2. **`isBelLocationValid`**: a LUT/DFF may only bind to a bel in its slice; a
   PIO to an IO bel.  Start permissive, tighten after the packer works.
3. **Packer**: `LUT4` -> `LUT{0..7}`, `DFF` -> `DFF{0..7}`, PIO -> `PIO{0..3}`.
   Use `SLICE{n}` clusters to keep the two LUTs and two DFFs of a slice together.
4. **Globals**: clock wires are not in the database yet; a first bring-up can
   route clocks as ordinary nets.
5. **Bitstream writer**:
   * start from a known-good blank `.bit` (one per device),
   * set LUT INIT fuses using the sidecar map,
   * set DFF config bits (not yet calibrated),
   * set PIO config bits (not yet calibrated),
   * set routing fuses (not yet calibrated),
   * append/recompute the 16-bit checksum (algorithm unknown).
   For a first hardware test, LUT-only + fixed routing from a hand-placed
   reference design is enough.

## Known gaps

* Column origin table (tile column -> bitstream column) is not calibrated; only
  the relative slice offsets (D=0, C=+8, B=+24, A=+32) are known.
* DFF / PIO / EBR / DSP fuse maps.
* Routing fuse map (pip -> bits).
* `.bit` checksum and `.bin` compression.
* Timing model.
