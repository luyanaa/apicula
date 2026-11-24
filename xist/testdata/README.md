# XIST test fixtures

Collected from HqFpga-XIST V3.1.0 (P022026) on **SL2-12E-8F256C**.

| File | Source | Contents |
|---|---|---|
| `SL2-12E-8F256C_udev.txt` | `'udev.report` | 40x51 tile grid, site names, resource counts |
| `SL2-12E-8F256C_pkg.txt` | `'udev.pkg.report` | 196 package pins: IO/site/tile/bank/desc |
| `SL2-12E_lut_{A,B,C,D}.json` | differential probes | LUT INIT minterm -> (row, col) for each slice |
| `SL2-12E_lut_all.json` | combined | the four maps + inferred F1 maps |
| `SL2-12E_columns.json` | column sweep | tile column -> bitstream block origin (slice A block at R10) |
| `counter.xpn` | `xpn.write` | routed physical netlist for a 4-bit counter |

Regenerate with:

```bash
python -m xist.cli udev --part SL2-12E-8F256C --out /tmp/xist
python -m xist.cli calibrate --part SL2-12E-8F256C --loc R10C10A --out /tmp/xist --save /tmp/xist/lut_A.json
# B/C/D: same command with R10C10B/C/D
```
