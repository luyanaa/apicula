# XIST (XiST / HqFpga) support work

This directory contains reverse-engineering and database-generation tooling for
the XiST **HqFpga** toolchain (智多晶, families SHARK / SEAL / SEALION).  It is
deliberately separate from `apycula` (Gowin): the two fabrics are closely
related but their bitstream geometry differs and the vendor data files have
nothing in common.

The target is a **nextpnr-himbaechel `xist` uarch**, in the same way Apicula
feeds `nextpnr-himbaechel --device GW1N-9`.  This directory currently provides
the device/bitstream/fuzz side; the C++ uarch comes next.

## Status

| Piece | State |
|---|---|
| `xist/bitstream.py` | **verified**: parse/write `.bit`, differential diff, LUT INIT encode/decode |
| `xist/device.py` | **verified**: `dv_list.xml`, `'udev.report`, `'udev.pkg.report` |
| `xist/hqfpga.py` | **verified**: batch flow runner |
| `xist/probe.py` | **verified**: locked-slice LUT probes + placed-netlist parser |
| `xist/lutmap.py` | **verified** per tile; the LUT minterm permutation is tile-position dependent (see below) |
| `xist/xpn.py` | **verified**: XPN signal/route parser |
| `xist/chipdb.py` | **skeleton**: 2040 tiles, 16 bels/PLC, 196 package pads, observed routing; emits `.bba` |
| `xist/bbalint.py` | **verified**: dangling label / push-pop checker |
| `xist/bbasm.py` | **verified**: Python bbasm; produced a valid chipdb blob (magic/version/width/height checked) |
| routing graph | **partial**: only pips observed in the XPN corpus |
| bitstream checksum / `.bin` compression | **unknown** |
| timing model | **unknown** (vendor STA is internal) |
| `nextpnr` uarch (`xist.cc`, packer, writer) | **route verified** for `a&b->o` on SL2-12E; full routing graph / bitstream TODO |

## Quick start

```bash
# 1. device grid + package pins
python -m xist.cli udev --part SL2-12E-8F256C --out /tmp/xist

# 2. calibrate the LUT INIT fuses of slice A (16 probes, ~2 min)
python -m xist.cli calibrate --part SL2-12E-8F256C --loc R10C10A \
    --out /tmp/xist --save /tmp/xist/lut_R10C10A.json

# 3. calibrate tile column origins (slice D, 2 probes per column)
python -m xist.cli columns --part SL2-12E-8F256C --row 10 \
    --save /tmp/xist/columns.json

# 4. emit a himbaechel chipdb (routing from any routed .xpn corpus)
python -m xist.cli chipdb --part SL2-12E-8F256C \
    --udev /tmp/xist/SL2-12E-8F256C_arch.txt \
    --pkg  /tmp/xist/SL2-12E-8F256C_pkg.txt \
    --lut  /tmp/xist/lut_R10C10A.json \
    --columns /tmp/xist/columns.json \
    --xpn design.xpn \
    --out-bba /tmp/xist/xist-SL2-12E.bba \
    --out-meta /tmp/xist/xist-SL2-12E.json
```

`xist-SL2-12E.bba` is the input of nextpnr's `bbasm`:

```bash
python -m xist.bbasm xist-SL2-12E.bba xist-SL2-12E.bin
# or, with nextpnr installed:
# bbasm --l xist-SL2-12E.bba xist-SL2-12E.bin
```

The Python assembler is validated against the expected `chip_info` header
(`magic=0x00ca7ca7`, `version=7`, `width x height` from the udev grid).

## What the vendor tool tells us

HqFpga is a Tcl shell (`hqfpga -cmd script.tcl`).  The useful subset:

```tcl
dv.setup SEALION SL2-12E-8F256C
design.analyze {top.v}
design.rtlsyn -top top
design.map
design.pack
design.place -seed 1
design.route
impl.bitgen.xist.sealion out.bit      ;# ASCII bitstream
xpn.write out.xpn                     ;# text physical netlist, per-net routes
nl.write out.v -phy -loc              ;# placed netlist with LOC comments
phycst.loc.set SLICE_0 R10C10A        ;# pin a slice (probe support)
'udev.report                          ;# tile grid + resource counts
'udev.pkg.report -file pkg.txt        ;# package pins
```

The device database (`common/device/xist/*.ebz`) is a gzip container with an
encrypted/compressed payload (entropy ~7.95 bit/byte), so it is *not* parsed.
Everything above is black-box observation, as Apicula did for Gowin before it
decoded the vendor `.dat`/`.fse` files.

## Bitstream

`.bit` is plain text: a 14-line header and a `Rows x Cols` bitmap with one ASCII
character per bit.  For SL2-12E: **1125 x 2376**, `Bits = 2673000`.  The last
bitmap line carries 16 extra bits (checksum; algorithm not yet identified).

`.bin` adds a `FF 00` magic and compresses the same data (43 KB for a 2.67 MB
bitmap for the counter design).  The compression is not reverse-engineered yet.

### LUT INIT encoding (calibrated)

Differential probing (16 one-minterm LUTs, locked slice) gives a full
`minterm -> (row, col)` map per slice.  For SL2-12E tile row 10, all four slices
live in bitmap rows 287/288, with block columns:

| slice | block col | layout |
|---|---|---|
| A | 1920 | two 8-bit rows, perm `(0,1,3,2,4,5,7,6)` |
| B | 1912 | even/odd minterms interleaved between rows |
| C | 1896 | 4-row-group interleave |
| D | 1888 | 4-row-group interleave, mirrored columns |

The layouts are genuinely different, which is why the tool records all 16
coordinates instead of a single permutation.  Verification: re-encoding each
`1 << i` into the `i = 0` probe reproduces the vendor bitstream **exactly**
(0 differing rows, all four slices).  F1 (the second LUT of a slice) is a
separate slot: at R10C10A it shares F0's column block with rows shifted by -2,
but its offset changes with tile position (see the caveat below).

The full maps are in `xist/testdata/SL2-12E_lut_{A,B,C,D}.json` and combined in
`SL2-12E_lut_all.json`.

The **block position** is regular:

```
row  = 29 * tile_row + row_rel
col  = column_origin[tile_col] + {"D":0, "C":8, "B":24, "A":32}[slice] + col_rel
```

`SL2-12E_columns.json` is a complete sweep of the tile columns (C2..C48 are PLC;
C0/C1/C49/C50 have no slice).  `xist.lutmap.slot_for()` implements the formula
and reproduces the C10 maps exactly.

**Important caveat: the minterm permutation is position dependent.**  Three
samples of the D-slice layout (column offsets for minterms 0..7) show it changes
with both row and column:

```
R10C10D : 7,5,3,1  7,5,3,1   (rows 288/287 in groups of four)
R10C19D : 7,5,3,1  7,5,3,1   (same as C10 at row 10)
R20C10D : 7,7,3,3  5,5,1,1   (rows 578/577 alternating)
R20C19D : 7,5,6,4  3,1,2,0
```

The permutation is therefore not a function of row alone or column alone; the
four samples are saved in `SL2-12E_lut_samples.json`.

The vendor packer maps the logical LUT inputs A/B/C/D to physical LUT inputs
differently depending on tile position.  F1 shares F0's column block at
R10C10 (verified for slices A and D); its offset at other positions has not been
verified yet.  Therefore a single calibration cannot be extrapolated across the
whole chip.  The tools support calibrating any tile (`calibrate --loc ...`);
building the full table needs one calibration per PLC tile (1504), which is a
parallel/overnight fuzz job, or a study of the mirroring pattern.

The reference-tile maps in `testdata/` are verified examples, not a device-wide
lookup.  If the mirroring pattern cannot be derived from a few samples, run the
campaign in parallel:

```bash
# 4 slices x 38 rows x 47 columns ~= 7100 calibrations, 16 probes each
python -m xist.cli tiles --part SL2-12E-8F256C --rows 2:40 --cols 2:49 \
    --jobs 8 --save /tmp/xist/tiles.json
```

Each worker gets its own directory, so the vendor tool instances do not clash.

## Routed demo (a & b -> o)

```bash
python -m xist.demo_route
```

The demo runs Yosys → JSON conversion → chipdb from `testdata/io.xpn` →
`nextpnr-himbaechel`.  Verified output:

```
Info: Routing 3 arcs.
Info: Routing complete.
Info: Program finished normally.
routed nets with ROUTING: 6
```

Two bring-up concessions are visible in the demo:

* the chipdb is built from the matching `io.xpn`, not a complete device graph;
* `nextpnr_json.py` rotates the LUT inputs by one (logical `I0/I1` → bel
  `I[1]/I[2]`) because the vendor packer maps this design to slice ports B/C.
  The real mapping is per-slice and still needs calibration.

## Chip database

`xist/chipdb.py` builds a `nextpnr.himbaechel_dbgen.chip.Chip`:

* one tile type per `'udev.report` token (`PLC`, `PIC_*`, `EBR`, `DSP`, ...),
* `PLC`: 4 slices x (2 `LUT4` + 2 `DFF`).  Wire names follow the vendor XPN
  convention (`F0..F7`, `DI0..DI7`, `Q0..Q7`, `A0..D7`), so pips parsed from XPN
  dumps attach directly to the bel pins,
* `PIC`: 4 `PIO` bels with PAD/I/O/OE,
* hard tiles: placeholder bels,
* routing: every `dst.src` pair in the XPN corpus becomes a pip (same tile) or a
  node (cross-tile long wire).

The routing graph is intentionally incomplete.  It currently contains the
resources used by the supplied XPN corpus, plus the long-wire segment chains
reconstructed from the vendor names.  It is not yet connected enough to route
even a one-LUT design: the CIB/track-switch part and the PIO/IOLOGIC path still
need mapping.  A production database needs a systematic pip fuzzer.

## Next steps

1. **Routing fuzzer**: for every observed wire, generate a design that routes a
   single net through it and diff the bitstream; label with XPN.  The vendor
   router is deterministic (`-seed 1`) and XPN gives wire names, which makes this
   much cheaper than Gowin's black-box fuzzing.
2. **Bitstream writer details**: identify the 16-bit checksum and the `.bin`
   compression (or bypass with an SVF/JTAG writer).
3. **nextpnr uarch**: the bring-up uarch is in `xist/nextpnr/uarch/xist/`
   (loads the chipdb, places LUT4/DFF/PIO).  Next: a real packer, PIO/IOLOGIC
   and inter-tile routing, then the bitstream writer.
4. **Yosys**: `synth_gowin`-derived `synth_xist` and a XIST cell library
   (the vendor sim library under `common/sim/verilog/XIST/` documents port
   names and parameters).
5. **Timing**: sample `ta.report` across constrained designs and fit per-cell /
   per-wire delays.

## Testing

```bash
python -m pytest tests/test_xist.py -q
```

The tests cover the bitstream round-trip, LUT codec, udev/pkg/XPN parsers,
the bba dangling-label checker and a small chipdb build.  Vendor-dependent
tests (probes, real udev export) are run manually against HqFpga.
