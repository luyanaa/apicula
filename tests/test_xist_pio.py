"""Tests for the edge PIO configuration model."""

from __future__ import annotations

from pathlib import Path

from xist.bitstream import Bitstream
from xist.pioconfig import (bits_from_equations, env_for, load_profile,
                            parse_program, pio_site)
from xist.program import program_pio
from xist.xpn import Comp, parse_comps

TESTDATA = Path(__file__).resolve().parent.parent / "xist" / "testdata"
PIO = TESTDATA / "SL2-12E_bfd_pio.json"

INPUT_PROGRAM = ("PADDI:PADDI IOBUF:::PULLMODE=DOWN,IO_TYPE=LVCMOS25, "
                 "DRIVE=NA,SLEWRATE=NA,CLAMP=ON, OPENDRAIN=OFF,"
                 "DIFFRESISTOR=OFF, DIFFDRIVE=NA,BANK_VCCIO=2.5, "
                 "HYSTERESIS=SMALL,PERSISTENT=OFF VREF:OFF PGMUX:INBUF "
                 "INRDMUX:PGMUX")


def test_parse_program():
    props, cfgs = parse_program(INPUT_PROGRAM)
    assert props["IO_TYPE"] == "LVCMOS25"
    assert props["PULLMODE"] == "DOWN"
    assert ("PADDI", "PADDI") in cfgs
    assert pio_site("PL2A") == "PIO_A"
    assert pio_site("PR26D") == "PIO_D"
    assert pio_site("SLICE_0") is None


def test_env_for_and_input_bits():
    eqs, meta = load_profile(PIO, "PIC_L5")
    comp = Comp("a", "PL2A", INPUT_PROGRAM)
    env = env_for([comp])
    assert ("PIO_A", "IOBUF.IO_TYPE") in env["props"]
    bits = {(r, c): v for r, c, v in bits_from_equations(eqs, [comp], 2, 2375)}
    # measured on the vendor probe: NG2 / PG2 / RD / CLAMP for slot A
    assert bits[(34, 2375)] is True
    assert bits[(38, 2375)] is True
    assert bits[(44, 2375)] is True
    assert bits[(47, 2375)] is True


def test_top_edge_bits():
    eqs, meta = load_profile(PIO, "PIC_T5")
    comp = Comp("a", "PT13A", INPUT_PROGRAM)
    env = env_for([comp])
    assert ("PIO_A", "IOBUF.IO_TYPE") in env["props"]
    bits = {(r, c): v for r, c, v in
            bits_from_equations(eqs, [comp], 1, 1788, 1108)}
    # measured on the top-edge probe (row base 1108)
    assert bits[(1109, 1777)] is True
    assert bits[(1109, 1751)] is True
    assert meta["rows"] == 6


def test_parse_comps_quoted_and_unquoted(tmp_path):
    p = tmp_path / "x.xpn"
    p.write_text('''design top
{
   comp "a"
   {
      logical
      {
         program "PADDI:PADDI "
                 "IOBUF:::PULLMODE=DOWN,IO_TYPE=LVCMOS25, \\"
                    "CLAMP=ON ";
      }
      site PL2A;
   }
   comp b
   {
      logical
      {
         program "PADDO:PADDO ";
      }
      site PR2B;
   }
}
''')
    comps = parse_comps(p)
    assert set(comps) == {"a", "b"}
    assert comps["a"].site == "PL2A"
    assert "PULLMODE=DOWN" in comps["a"].program
    assert comps["b"].site == "PR2B"


def _make_bs(rows=60, cols=2400):
    return Bitstream(
        header_lines=["XIST ASCII Bitstream", "Version:          1.0",
                      f"Rows:             {rows}", f"Cols:             {cols}"],
        header={"Rows": str(rows), "Cols": str(cols)},
        pre_block=["1010"],
        rows=["0" * cols for _ in range(rows)],
        footer_block=["1111"],
        extra16="0" * 16,
    )


def test_program_pio(tmp_path):
    xpn = tmp_path / "x.xpn"
    xpn.write_text('''design top
{
   comp "a"
   {
      logical
      {
         program "PADDI:PADDI IOBUF:::PULLMODE=DOWN,IO_TYPE=LVCMOS25, DRIVE=NA,SLEWRATE=NA,CLAMP=ON, OPENDRAIN=OFF,DIFFRESISTOR=OFF, DIFFDRIVE=NA,BANK_VCCIO=2.5, HYSTERESIS=SMALL,PERSISTENT=OFF VREF:OFF PGMUX:INBUF INRDMUX:PGMUX";
      }
      site PL2A;
   }
}
''')
    eqs, _meta = load_profile(PIO, "PIC_L5")
    bs = _make_bs()
    n = program_pio(bs, xpn, eqs, 2, 2375, "L")
    assert n > 0
    assert bs.rows[34][2375] == "1"   # NG2_N_A
    assert bs.rows[44][2375] == "1"   # RD_A
