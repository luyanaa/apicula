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
| `xist/lutmap.py` | **superseded**: the physical SRAM layout is a tile-type constant; the observed per-tile variation is vendor LUT pin swapping (see "LUT INIT" below) |
| `xist/xpn.py` | **verified**: XPN signal/route parser |
| `xist/chipdb.py` | **skeleton**: 2040 tiles, 16 bels/PLC, 196 package pads, observed routing; emits `.bba` |
| `xist/bbalint.py` | **verified**: dangling label / push-pop checker |
| `xist/bbasm.py` | **verified**: Python bbasm; produced a valid chipdb blob (magic/version/width/height checked) |
| routing graph | **bring-up**: complete CIB + long-wire mesh; arbitrary designs route |
| pip -> fuse map | **complete for CIB/long-wire**: vendor BFD data, 3315 arcs / 1976 long-wire across 56 routing tile types in 28 profiles (`SL2-12E_bfd_profiles.json`); validated on 305 PLC + 331 edge-tile bitstream instances |
| bitstream checksum | **decoded**: CRC-16/0x8005 over packed bits XOR 0x1FE2 |
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

Profiles cover every 29-row routing tile type: the 2-column centre tiles, the
29x48 CIB family (PLC, CIB_CFG*, CIB_DSP*, CIB_EBR*) and the 29x59 edge/PIC
types (PIC_L*, PIC_R*, LLC5PIC, LRC5PIC, ULC0, URC1_NO_DTR, CIB_EBR*_END*).
Tile types with an identical arc->fuse map share one profile:

```
testdata/SL2-12E_bfd_profiles.json   28 profiles, 56 tile types
                                     3315 distinct arcs, 1976 long-wire
testdata/SL2-12E_bfd_PLC.json        PLC in detail (2500 arcs)
```

The 59-column profiles add 157 arcs that the 48-column CIB family does not
have (clock/PLL and junction arcs); they add no new long-wire names, so the
long-wire union stays at 1976.  The 2-column centre profiles add 490 non-long
arcs.

**Validation**: the BFD equations were evaluated for routed `io_pass`
bitstreams (slices locked at many tile locations) and compared with the vendor
bitstream.

48-column PLC tiles (305 instances):

```
designs 305 perfect 305
positions 344040 match 344040 (100.000%) predicted-one 7516
```

59-column edge tiles (the mapping is per tile column; the bit column base is
2375 for tile column 1 and 60 for tile column 49):

```
--validate-column 1  -> designs 178 perfect 178
                        206124/206124 (100.000%)
--validate-column 49 -> designs 153 perfect 152
                        177170/177174 (99.998%)
```

The single tc-49 outlier has 4 wrong bits and is the one sampled tile that uses
a different 59-column profile; per-location profile selection removes it.  The
48-column non-PLC CIB types (CIB_CFG/CIB_DSP/CIB_EBR at EBR/D/CTEST locations)
were checked with the same rule and agree on 4220979/4228402 bits (99.82%);
their profiles share most routing-fuse positions, so profile choice, not the
row/column mapping, accounts for the residual.

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

### BFD routing in the chipdb

`xist/bfd_chipdb.py` installs a profile into a chipdb tile type:
`build_chip(..., bfd_profiles={"PLC": "testdata/SL2-12E_bfd_PLC.json"})` adds
every BFD wire and adds each `arcval(src,dst)` as a directed pip.  The fuse
positions travel in the sidecar (`write_meta`):

```
"routing_fuses": {"PLC": {"src->dst": [{"row":8,"col":16,"fuse":"..."}]}}
"row_rule": "bit_row = 29 * (tile_row - 1) + ram_row"
"col_rule": "bit_col = bitcol_base[tile_col] - ram_col"
"bitcol_base": {"19": 1500, ...}
```

`replace_cib=True` (demo: `--bfd-replace`) drops the synthetic PLC crossbar so
the router can only use vendor pips.

The edge ring is split into contextual tile types because one UDEV token can
cover physically different CIB blocks:

```
CTEST, row 1/38          -> CTEST_CFG   profile CIB_CFG
CTEST, EBR/DSP row bands -> CTEST_EBR   profile CIB_EBR
CTEST, column 1 / 49     -> CTEST_L/R   profile CIB_EBR0_END0 / CIB_EBR2_END1
EBR   body / column edge -> EBR_CIB, EBR_L/R
D0..D8 (row 16)          -> DSP_CIB     profile CIB_DSP, DSP_L/R at the edges
```

`default_bfd_profiles()` installs this map; the sidecar then contains one
`routing_fuses` table per contextual type (PLC 2500, CTEST_CFG 2476,
EBR_CIB 2500, DSP_CIB 2476, CTEST_L 2556, CTEST_R 2532, ...).

Measured on routed designs (`--bfd-replace`):

```
mux4     PLC pips 29, fused 29
io_pass  PLC pips 23, fused 23
wide_eq  PLC 43/43, CTEST_CFG 3/3, CTEST_EBR 1/1
```

Edge CIB pips are now fuse-backed.  What remains synthetic:
`PIC` (column 0), `PIC_right` (column 50), `PIC_top/bot` and the `-12k`
corner tiles.  Their configuration lives in the neighbouring 59-column block,
and their equations include negated arcs and wire guards, e.g.

```
MC1_INDEL_0 = !arcval(JPADDIA_PIO,JDIA) && wire(JDIA)
```

The parser, profile export and writer now support that form:
`BfdTile.special_fuses()` records `{arc, guard, invert}`, the profile JSON
carries a `special_fuses` section, the chipdb attaches it to the
data-direction pip, and `program_routing` evaluates
`fuse = !used(arc) && used(guard)`.

Enabling it for PIC is still blocked by the mapping, not the semantics.  A
controlled A/B probe (same design, input pad D4/PL2A vs D1/PL5D) isolated the
left slot-A bit at `(29*(tile_row-1)+2, 2361)` (column base 2371), but over the
routed corpus the literal rule matches only 80% (slot A) to 100% (slots B-D)
depending on how the XPN pair orientation is read, and the vendor's left-edge
tile varies by row (`PIC_L5`, `PIC_L0_DUMMY`, `LLC5PIC`, `CIB_EBR0_END0...`)
with different INDEL rows/columns.  A per-row, per-slot probe campaign is
needed before these fuses can be trusted in a writer.

### PIO probe campaign (edge pads)

87 controlled probes were run (left/right edge, slots A-D, every row that has a
package pad).  Each probe compares an input pad at the site (`o=~a`) with an
output pad at the same site (`a=~b, o=~b`), same slice, and diffs the 29x59 pad
tile block.

Result: the input-mode bit pattern is identical for every probed row of a given
(side, slot).  Example (row offset within the tile, absolute bit column):

```
L A: (2,2361) (3,2363) (5,2375) (9,2375) (15,2375) (18,2375)   11/11 rows
L B: (2,2364) (3,2361) (5,2374) (9,2374) (15,2374) (18,2374)   12/12
R A: (0,58)   (5,2)   (9,2)   (15,2)   (18,2)                  12/12
```

Checked against the routed corpus, the input patterns reproduce 1283/1289 bits
(99.53%; the 6 differences are three designs with a different IO program).

Output-mode patterns are not stable: the A/C diff also contains the routing to
the output pad, which moves with placement.  Of the measured bits, only the
INDEL appears in the BFD routing equations (`!arcval(PIO,JDI) && wire(JDI)`);
the rest are PIO configuration (direction, IO standard, pull), which the BFD
routing export does not model.

The raw measurements are in `testdata/SL2-12E_pio_probe.json`.

### LUT INIT and slice configuration

Two more configuration regions are decoded from the same BFD dump.

**Slice control (DFF/ALU/LUT control)**: `xist/sliceconfig.py` parses the XPN
slice program (`MODE:LOGIC`, `K0::H0=(A*B)`, `REG0:::REGSET=RESET,CLKDELAY=DEL0:SD=1`,
`CLKMUX:CLK:::0=0,CLK=#SIG`, `CEMUX`, `LSRMUX`, `SRMODE`, `REGMODE`, `GSR`) and
evaluates the PLC `XPFU` configuration equations.  Only slices instantiated in
the tile are driven.  Validated read-only on the routed corpus:

```
370 tile groups, 27010 / 27010 slice configuration bits (100.000%)
```

**LUT INIT**: the BFD encodes the truth-table fuses as
``memory(SLICE_D, bank, addr)``, which gives both the position and the SRAM
word address directly - no probe campaign needed:

```
XPFU_SR/XXPFU_SLICE_SR_3/XXLOGICMUX16_0/XXLUT0/Q0 = (comp(SLICE_D) && !memory(SLICE_D,0,0))
```

`xist/bfdmap._lut_init_table` exports these as
``lut_init[site][bank][minterm] = (ram_row, ram_col)`` in
`testdata/SL2-12E_bfd_PLC.json`.  Bank 0 is K0/H0, bank 1 is K1/H1, and the
minterm index uses the natural variable order (A=bit0).  The XPN carries the
function itself (`K0::H0=(D*(A*B))`), so `xist/lutinit.py` evaluates it and
writes the complemented fuses.  Validated on the routed corpus:

```
698 slice instances, 21664 / 21664 LUT INIT bits (100.000%)
```

#### The per-tile LUT "permutation" was a probe artefact

An earlier note here said the LUT minterm permutation depends on tile position
and would need a per-tile campaign.  That was wrong, and the probe framework
itself was the reason.

`run_lut_probe` synthesised `xsLUT4 u0 (.A(a),.B(b),.C(c),.D(d),.Z(o0))` with
`defparam u0.init = 16'hHERE` and then assumed the vendor kept that A..D
correspondence when it mapped the LUT into the PLC slice.  It does not
guarantee that.  Reading the XPN back from the same probe shows the vendor
re-encodes the function with a different input association per tile:

```
R10C10D  K0 = (B*(A*(D*C)))
R20C19D  K0 = (A*(D*(B*C)))
R20C10D  K0 = (B*(C*(D*A)))
R30C20D  K0 = (D*(A*(C*B)))
```

All four are `A·B·C·D`, i.e. the same logic, but the letter that the vendor
prints for each physical input differs.  The probe compared physical fuses
against a logical truth table without accounting for that relabelling, so the
"permutation" it measured was the vendor's LUT input re-encoding, not the SRAM
layout.

The SRAM layout itself is a **tile-type constant**.  The BFD gives it directly:

```
memory(SLICE_D, 0, addr) -> bit (29*(row-1) + base_row - (addr & 1),
                                  col_base - base_col - (addr >> 1))
```

with `base_row = 28` and `base_col = 37` for slice D bank 0, identical for every
PLC on the chip.  Using that identity map together with the function parsed from
the tile's own XPN program reproduces the vendor bitstream exactly:

```
R10C10D  (B*(A*(D*C)))   init 0x8000   16/16 bits
R20C19D  (A*(D*(B*C)))   init 0x8000   16/16 bits
R20C10D  (B*(C*(D*A)))   init 0x8000   16/16 bits
R10C30D  (B*(A*(D*C)))   init 0x8000   16/16 bits
R30C20D  (D*(A*(C*B)))   init 0x8000   16/16 bits
```

and on the routed corpus (698 slice instances, functions taken from the XPN):

```
LUT INIT bits 21664 / 21664 (100.000%)
```

So no per-tile LUT calibration is needed at all.  What a writer needs is the
slice function as the vendor expresses it, which the XPN already provides as
`K0::H0=...` / `K1::H1=...`.  `xist/lutinit.py` evaluates those expressions and
writes the BFD identity map.

The probe framework is still useful for anything the BFD does not describe, but
its results must be read together with the probe's own XPN, never against an
assumed pin order.  `run_lut_probe` now keeps that XPN (`{name}.xpn`).

### .bit assembly

`xist/build.py` overlays everything that is decoded onto a template `.bit`:

```bash
python -m xist.build --base template.bit --xpn design.xpn --out design.bit
```

It walks every tile in the device grid, picks the contextual BFD profile
(`PLC`, `CIB_CFG`, `CIB_EBR`, `CIB_DSP`, the 59-column edge profiles), programs
the routing muxes and LUT INIT, writes the slice configuration, applies the PIO
configuration, and finally recomputes the checksum.

Running it with a vendor bitstream as both template and source is a strong
check: every bit it writes must already be correct.  On 60 routed designs the
difference from the vendor bitstream is:

```
total residual 1421 of 160380000 bits = 0.000886%
per design: best 16, worst 64, mean 23.7
```

The residual is concentrated in three places, all of them still synthetic:
the 59-column spine tiles (`CENTER*_12K`, whose 2-column bit mapping is not
calibrated), `CTEST_CFG`/`CTEST_EBR` (a row-1/38 profile subset), and a handful
of same-track long-wire aliases.  Routing, slice configuration, LUT INIT, PIO
configuration and the checksum are bit-exact.

### PIO configuration model

The probe bits are not routing arcs; they are `property()`/`config()` rules in
the BFD, e.g.

```
MC12_RD_A    = ... && config(PIO_A,PADDI,"PADDI")
MC12_CLAMP_A = property(PIO_A,IOBUF.CLAMP,"ON") && config(PIO_A,PADDI,"PADDI")
```

`xist/bfd_expr.py` parses/evaluates that Boolean grammar, `xist/xpn.py`
`parse_comps()` reads the component program strings (`PADDI:PADDI`,
`IOBUF:::PULLMODE=DOWN,IO_TYPE=LVCMOS25,...`), and `xist/pioconfig.py` maps the
placement site (`PL2A`) to the BFD site (`PIO_A`) and returns every configuration
fuse for the tile.  The profiles are stored in
`testdata/SL2-12E_bfd_pio.json` (PIC_L5 / PIC_R5, 220/236 fuses).

Bit column base: left edge 2375, right edge 60; row rule unchanged.

Validation, read-only (evaluate the equations and compare with the vendor
bitstream) and through the writer (`program_pio` into an unrelated `.bit`):

```
3353 edge tile groups
322991 / 322991 PIO configuration bits (100.000%)
```

This covers all edge pads: `PIC` (column 0), `PIC_right` (column 50), and the
top/bottom edges.  The top/bottom pads use the 6-row `PIC_T5` / `PIC_B5`
profiles stored at the end of the bitstream:

```
top    row base 1108 (rows 1108..1113)
bottom row base 1102 (rows 1102..1107)
column delta: T/B column 7 -> -2, T/B column 45 -> -1, otherwise 0
```

### Bitstream checksum

The 16 characters appended to the last bitmap row are a CRC-16 of the
bit-packed configuration:

```
poly    0x8005 (non-reflected, MSB first)
init    0
data    1125 x 2376 bits, 8 bits per byte, MSB first, all rows (incl. zero last row)
xorout  0x1FE2
write   16 '0'/'1' characters (MSB first)
```

The polynomial was derived from single-bit probes: clearing `CLAMP` on a PAD
flips exactly one bitmap bit, and the resulting checksum deltas for adjacent
columns satisfy the 0x8005 shift recurrence (`0xFD6C -> 0x7ADD -> 0xF5BA ->
0x6B71`).  Verification: 371/372 vendor bitstreams match; the one exception is
`h0_from_h1.bit`, a file patched earlier without recomputing the trailer.

`Bitstream.checksum()/verify_checksum()/update_checksum()` implement it, and
`xist.apply` / `xist.program` recompute the trailer after writing.

### Applying PIO configuration

```
python -m xist.apply --base template.bit --xpn design.xpn --out out.bit
python -m xist.apply --base template.bit --xpn design.xpn --check vendor.bit
```

`xist/apply.py` programs every edge pad's PIO configuration from the XPN into a
`.bit` template; `--check` compares the written positions with a vendor
bitstream.  Example (c0 design into an unrelated template): 863/863 bits match.
The C++ uarch writer is still blocked on the unknown `.bit` checksum; this
Python path uses the same sidecar data (`pio_config`).

### -12k corner tiles

UDEF tokens such as `R1C27-12k`, `R16C28-12k`, `R22C1-12k` are the
`CENTER6_12K`..`CENTERA_12K` / `CFG0_12K` / `DSP*_12K` spine tiles, not PIO.
They carry long-wire/CIB routing (the corpus routes `H06W`/`V06S` wires through
them) and some have clock/DCC sites.  Their routing profiles are in
`SL2-12E_bfd_profiles.json`, but their 2-column / 6-row bit mapping is not
calibrated, so they remain synthetic in the router.

DFF-heavy designs (`dff_chain`, `counter8`, `shift8`) do not route in BFD-only
mode: their data input is the slice output mux (`OFX -> DI`), so they require
LUT/DFF packing that the bring-up uarch does not do yet.  `--bfd` (additive)
still routes the full corpus 10/10.

Tile columns 0 and 50 have no bitstream columns of their own: their I/O cells
are configured through the neighbouring 59-column block (hence the base for
those columns is absent).  Fuses whose equation mixes `config()`/`property()`
with `arcval()` depend on slice configuration and are not driven by the
arc-only writer.

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
