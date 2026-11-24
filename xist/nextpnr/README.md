# Minimal nextpnr-himbaechel `xist` uarch

This is the bring-up uarch used to check that the chipdb produced by
`xist.chipdb` loads in nextpnr and that LUT4/DFF/PIO cells can be placed.

It is deliberately small:

* loads `xist/chipdb-SL2-12E-8F256C.bin` (pass `--chipdb` for a local file),
* defines bel buckets/validity for `LUT4`, `DFF` and `PIO`,
* does **not** pack constants or pair LUT/DFF cells yet,
* does **not** write a bitstream,
* the routing graph is the sparse set of pips observed in the vendor XPN
  corpus, so routing a real design will fail.

## Apply to a nextpnr checkout

```bash
cd /path/to/nextpnr
cp -r /path/to/apicula/xist/nextpnr/uarch/xist himbaechel/uarch/
# add xist to HIMBAECHEL_UARCHES in himbaechel/CMakeLists.txt
cmake -S . -B build -G Ninja -DARCH=himbaechel -DHIMBAECHEL_UARCH=xist \
      -DBUILD_TESTS=OFF -DBUILD_GUI=OFF -DBUILD_PYTHON=OFF
ninja -C build
```

## Run

```bash
# convert a Yosys netlist (synth_gowin JSON) to the XIST cell set
python -m xist.nextpnr_json top.json top_xist.json

build/nextpnr-himbaechel \
    --device SL2-12E \
    --chipdb /path/to/xist-SL2-12E-8F256C.bin \
    --json top_xist.json \
    --place --write placed.json
```

Expected result today: the chipdb loads, cells are placed on LUT/DFF/PIO bels.
Routing and bitstream generation still need the real routing graph and fuse
maps (see `doc/xist.md`).
