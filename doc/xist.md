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
| `xist/lutmap.py` | **verified** for SL2-12E slice A; B/C/D calibrated by the same tool |
| `xist/xpn.py` | **verified**: XPN signal/route parser |
| `xist/chipdb.py` | **skeleton**: tiles/bels + observed routing; emits `.bba` |
| `xist/bbalint.py` | **verified**: dangling label / push-pop checker |
| `xist/bbasm.py` | **verified**: Python bbasm; produced a valid chipdb blob (magic/version/width/height checked) |
| routing graph | **partial**: only pips observed in the XPN corpus |
| bitstream checksum / `.bin` compression | **unknown** |
| timing model | **unknown** (vendor STA is internal) |
| `nextpnr` uarch (`xist.cc`, packer, writer) | **not started** |

## Quick start

```bash
# 1. device grid + package pins
python -m xist.cli udev --part SL2-12E-8F256C --out /tmp/xist

# 2. calibrate the LUT INIT fuses of slice A (16 probes, ~2 min)
python -m xist.cli calibrate --part SL2-12E-8F256C --loc R10C10A \
    --out /tmp/xist --save /tmp/xist/lut_R10C10A.json

# 3. emit a himbaechel chipdb (routing from any routed .xpn corpus)
python -m xist.cli chipdb --part SL2-12E-8F256C \
    --udev /tmp/xist/SL2-12E-8F256C_arch.txt \
    --lut /tmp/xist/lut_R10C10A.json \
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
(0 differing rows, all four slices).  F1 (the second LUT of a slice) is the F0
map shifted by -2 rows for slice A; B/C/D are inferred the same way and still
need a two-LUT probe to confirm.

The full maps are in `xist/testdata/SL2-12E_lut_{A,B,C,D}.json` and combined in
`SL2-12E_lut_all.json`.

What is *not* yet known: the mapping from tile column index to the bitstream
column origin (the pitch alternates between 47 and 48 bits).  A column sweep
(slice D probe at every C, ~50 runs) will produce that table.

## Chip database

`xist/chipdb.py` builds a `nextpnr.himbaechel_dbgen.chip.Chip`:

* one tile type per `'udev.report` token (`PLC`, `PIC_*`, `EBR`, `DSP`, ...),
* `PLC`: 4 slices x (2 `LUT4` + 2 `DFF`) with slice-local wires,
* `PIC`: 4 `PIO` bels with PAD/I/O/OE,
* hard tiles: placeholder bels,
* routing: every `dst.src` pair in the XPN corpus becomes a pip (same tile) or a
  node (cross-tile long wire).

The routing graph is intentionally incomplete: it only contains resources the
vendor router used in the supplied designs.  A production database needs a
systematic pip fuzzer (see below).

## Next steps

1. **Routing fuzzer**: for every observed wire, generate a design that routes a
   single net through it and diff the bitstream; label with XPN.  The vendor
   router is deterministic (`-seed 1`) and XPN gives wire names, which makes this
   much cheaper than Gowin's black-box fuzzing.
2. **Bitstream writer details**: identify the 16-bit checksum and the `.bin`
   compression (or bypass with an SVF/JTAG writer).
3. **nextpnr uarch**: `himbaechel/uarch/xist/{xist.cc,pack_*.cc,constids.inc}`
   plus the `bbasm` build rule; start with LUT/DFF/PIO only.
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
