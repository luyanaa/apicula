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
| routing graph | **bring-up**: complete CIB + long-wire mesh; arbitrary designs route |
| pip -> fuse map | **complete for CIB/long-wire**: vendor BFD data, 2668 arcs / 1976 long-wire across 22 CIB tile types (`SL2-12E_bfd_profiles.json`); PLC validated on 305 bitstreams |
| bitstream checksum / `.bin` compression | **unknown** |
| timing model | **unknown** (vendor STA is internal) |
| `nextpnr` uarch (`xist.cc`, packer, writer) | **route verified** for arbitrary LUT/DFF/ALU/MUX/IO designs on the bring-up graph; vendor fuse map / bitstream TODO |

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

## Generic routing (bring-up graph)

The current database does **not** need a matching XPN.  Each tile gets a
32-wire CIB crossbar (bel pins, PIO pads, ALU and long wires) and a complete
1-row V/H long-wire mesh; nextpnr can then route arbitrary LUT/DFF/ALU/MUX/IO
designs:

```bash
python -m xist.demo_any my_design.v
#   Info: Routing ...
#   Info: Routing complete.
#   Info: Program finished normally.
```

Verified in this session (all `Routing complete` / rc=0):

```
io_pass  xbar4  wide_eq  mux4  mux8  lut_chain  fanout8  shift8
dff_chain  counter8  adder4  add16  lfsr8  pipe4  cmp8  mult4
```

This graph is a **bring-up model**, not the vendor fuse map: the CIB is a full
crossbar, and the long-wire mesh uses 1-row segments (the vendor uses 2-row
segments and specific mux fuses).  It proves the bels/packer/placer/router
integration; the bitstream writer still needs the real pip->fuse map.

## Pip -> fuse correlation (first results)

`xist/fusemap.py` runs the same netlist with different pin assignments (fixed
slice, varying routing) and correlates bitstream bits with XPN pip presence:

```bash
python -m xist.fusemap --bits w*.bit --xpns w*.xpn --out pipfuses.json
```

Sample run (a&b->o, 22 pin variants, slice R20C19D locked):

```
variant bits 873, signatures 80
pips 483, signatures 62
unique exact matches 8
  ('R20C19_A6_SLICE', 'R20C19_A6') -> (578, 1456)
  ('R20C19_B6_SLICE', 'R20C19_B6') -> (577, 1457)
  ('R20C19_C6_SLICE', 'R20C19_C6') -> (577, 1458)
  ('R20C19_D6_SLICE', 'R20C19_D6') -> (577, 1460)
  ...
```

The first candidates are in `testdata/SL2-12E_pip_fuses_sample.json`; the
expanded fuzz table (`SL2-12E_pip_fuses_expanded.json`) has **231 candidates**
(135 long-wire, 96 slice/CIP) from 63 tile locations.  Each location used
12 pin variants with the slices locked, a 70/30 fit/holdout split and only
holdout-clean candidates were merged.

`xist/campaign.py` scales the fuzz campaign to any tile list:

```bash
python -m xist.campaign --locations auto:1504 --variants 4 --jobs 8 \
    --out pip_fuses.json
```

`auto:1504` selects all PLC tiles.  That literal campaign is no longer needed:
the vendor tool stores the complete routing fuse map, and `'bfd.dumpbfd` dumps
it in 18.8 MB.  See the next section.

### Complete CIB/long-wire fuse map from the vendor BFD

```
dv.setup SEALION SL2-12E-8F256C
'bfd.dumpbfd /tmp/SL2-12E.bfd
exit
```

`xist/bfd.py` parses each tile type's 29x48 Ram fuse grid and its Boolean
equations (``fuse = arcval(source, destination) || ...``).  `xist/bfdmap.py`
exports the routing tables and calibrates tile-local Ram addresses to absolute
bitstream bits:

```
bit_row = 29 * (tile_row - 1) + ram_row
bit_col = column_base[tile_col] - ram_col
```

`column_base` is the slice-D block origin plus 44, with a measured per-column
correction (`testdata/SL2-12E_bitcol_base.json`).

Complete PLC (the CIB tile type):

```
arcs 2500 (1920 touching a long wire), fuses 1392, routing fuses 1128
```

Profiles cover the whole 29x48 CIB family (PLC, CIB_CFG*, CIB_DSP*, CIB_EBR*),
22 tile types with 2668 distinct arcs, 1976 of them long-wire:

```
testdata/SL2-12E_bfd_profiles.json   (4 profiles, compact)
testdata/SL2-12E_bfd_PLC.json        (PLC in detail)
```

**Validation**: the BFD equations were evaluated for 305 routed `io_pass`
bitstreams (slices locked at 15 tile locations) and compared with the vendor
bitstream.  All routing-fuse predictions match:

```
designs checked 305, perfect 305
344040 fuse predictions, 344040 matches (100.000%), 7516 predicted set
```

`xist/program.py` can write them:

```python
from xist.bfd import parse
from xist.bfdmap import load_calibration
from xist.program import program_routing
tiles = parse("SL2-12E.bfd")
cal = load_calibration()
program_routing(bs, "design.xpn", tiles["PLC"], cal, 20, 19)
```

Starting from an unrelated `.bit`, programming the routing region from a
design's XPN reproduces the vendor routing bits exactly (305/305 designs).

As a cross-check on completeness, 829 of the 840 distinct vendor-named
long-wire pips seen in the routed corpus are present verbatim in the PLC arc
set (98.7%).  The other 11 are same-track reversal pairs (`V02N0400` ->
`V02S0401`) that act as wire aliases; the corpus also contains 103 `J*` wires
invented by the nextpnr bring-up model, which have no vendor fuse.

The 59-column PIC/LLC/ULC tile types have their own wider Ram grid; they are
parsed but not part of the exported CIB profiles.  Fuses whose equation mixes
`config()`/`property()` with `arcval()` depend on slice configuration and are
not driven by the arc-only writer.

**Holdout validation**: 8 new pin variants (not used in the fit) predict all
8 candidates **8/8**, including the inverted-polarity cases.

`xist/program.py` applies the fuse table to a `.bit`:

```bash
python -m xist.program --base h0.bit --xpn h1.xpn \
    --map xist/testdata/SL2-12E_pip_fuses_sample.json --out h0_from_h1.bit
```

Applying `h1`'s pips to `h0` makes all 8 candidate bits match `h1` (8/8).

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
