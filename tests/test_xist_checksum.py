"""Tests for the XIST .bit trailer checksum.

Algorithm (verified on 371 vendor bitstreams):
    CRC-16, polynomial 0x8005, non-reflected (MSB first), init 0,
    over the bit-packed bitmap (8 bits per byte, MSB first, all rows),
    then XOR 0x1FE2.  The result is written as 16 '0'/'1' characters.
"""

from __future__ import annotations

from xist.bitstream import Bitstream


def make_bs(rows: list[str]) -> Bitstream:
    nrows, ncols = len(rows), len(rows[0])
    return Bitstream(
        header_lines=["XIST ASCII Bitstream", "Version:          1.0",
                      f"Rows:             {nrows}", f"Cols:             {ncols}"],
        header={"Rows": str(nrows), "Cols": str(ncols)},
        pre_block=[],
        rows=list(rows),
        footer_block=[],
        extra16="",
    )


def crc8005_bitwise(data: bytes) -> int:
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x8005) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def test_checksum_roundtrip():
    rows = ["01" * 4, "10" * 4, "0" * 8]
    bs = make_bs(rows)
    bs.update_checksum()
    assert len(bs.extra16) == 16
    assert set(bs.extra16) <= {"0", "1"}
    assert bs.verify_checksum()


def test_checksum_matches_bitwise_reference():
    rows = ["01011010", "10100101", "11110000", "00001111"]
    bs = make_bs(rows)
    expected = crc8005_bitwise(bs.packed_bytes()) ^ 0x1FE2
    assert bs.checksum() == expected


def test_checksum_changes_with_data():
    rows = ["0" * 8, "0" * 8]
    bs = make_bs(rows)
    bs.update_checksum()
    first = bs.extra16
    bs.rows[0] = "1" + bs.rows[0][1:]
    bs.update_checksum()
    assert bs.extra16 != first
    assert bs.verify_checksum()


def test_apply_then_checksum(tmp_path):
    from xist.apply import apply_pio
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
    rows = ["0" * 2400 for _ in range(1125)]
    bs = make_bs(rows)
    apply_pio(bs, xpn)
    bs.update_checksum()
    assert bs.verify_checksum()
