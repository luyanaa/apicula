import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xist.bitstream import (Bitstream, LutSlot, decode_lut_init,
                            encode_lut_init, diff_positions)
from xist.device import parse_udev_report, parse_pkg_report
from xist.xpn import parse_xpn


def make_bs(rows=40, cols=40):
    return Bitstream(
        header_lines=["XIST ASCII Bitstream", "Version:          1.0",
                      f"Rows:             {rows}", f"Cols:             {cols}"],
        header={"Rows": str(rows), "Cols": str(cols)},
        pre_block=["1010"],
        rows=["0" * cols for _ in range(rows)],
        footer_block=["1111"],
        extra16="0" * 16,
    )


def test_bitstream_roundtrip(tmp_path):
    bs = make_bs()
    bs.rows[3] = "1" * 40
    p = tmp_path / "a.bit"
    bs.write(str(p))
    other = Bitstream.read(str(p))
    assert other.nrows == bs.nrows
    assert other.ncols == bs.ncols
    assert other.rows == bs.rows
    assert other.extra16 == bs.extra16
    assert other.to_bytes() == bs.to_bytes()


def test_lut_codec_roundtrip():
    bs = make_bs()
    slot = LutSlot.byte_split(1, "F0", block_col=10)
    for init in (0x0001, 0x8000, 0x6996, 0xC0C0, 0xFFFF):
        encode_lut_init(bs, init, slot)
        assert decode_lut_init(bs, slot) == init


def test_diff_positions():
    a = make_bs()
    b = make_bs()
    b.rows[5] = "1" + b.rows[5][1:]
    b.rows[7] = b.rows[7][:3] + "1" + b.rows[7][4:]
    d = diff_positions(a, b)
    assert d == {5: [0], 7: [3]}


UDEV = """[V]: Grid: (2, 3)
0\t1\t2\t
0\t.\tPIC_top\t.
1\tPIC\tPLC\tPLC
0\t.\tT7\t.
1\tL2\t\t
Count SLICE : 8
Count PIO : 4
"""


def test_parse_udev():
    rep = parse_udev_report(UDEV)
    assert rep.rows == 2 and rep.cols == 3
    assert rep.tiles[1][1] == "PLC"
    assert rep.sites[0][1] == "T7"
    assert rep.counts["SLICE"] == 8
    assert rep.tiles_of("PLC") == [(1, 1), (1, 2)]


PKG = "IO = A4, site = X, tile = R1C2, bank = 1, desc = GPIO\n"


def test_parse_pkg():
    pins = parse_pkg_report(PKG)
    assert pins == [{"io": "A4", "site": "X", "tile": "R1C2",
                     "bank": "1", "desc": "GPIO"}]


XPN = '''design t
{
   signal "n1"
   {
      signal-pins
         (SLICE_0, F0),
         ("q[1]_MGIOL", OPOS);
      route
         R20C19_F4_SLICE.R20C19_F4,
         R38C19_JA2.R39C19_JOPOSC_BSIOLOGIC,
   }
}
'''


def test_parse_xpn(tmp_path):
    p = tmp_path / "t.xpn"
    p.write_text(XPN)
    sigs = parse_xpn(p)
    assert set(sigs) == {"n1"}
    assert sigs["n1"].pins == [("SLICE_0", "F0"), ("q[1]_MGIOL", "OPOS")]
    assert sigs["n1"].route[0] == ("R20C19_F4", "R20C19_F4_SLICE")
    assert sigs["n1"].route[1] == ("R39C19_JOPOSC_BSIOLOGIC", "R38C19_JA2")


def test_chipdb_smoke(tmp_path):
    from xist.chipdb import build_chip
    from xist.bbalint import check
    from xist.device import parse_udev_report

    rep = parse_udev_report(UDEV)
    chip = build_chip("SL2-12E-8F256C", rep, xpn_paths=[])
    bba = tmp_path / "x.bba"
    chip.write_bba(bba)
    assert check(bba) == []
    assert any(b.bel_type.index for t in chip.chip.tile_types for b in t.bels)


def test_bbasm_smoke(tmp_path):
    from xist.bbasm import assemble, dump_chip_info
    bba = tmp_path / "t.bba"
    slices = "".join("slice data 1\n" for _ in range(6))
    bba.write_text(f"""push blob
ref info
label data
u32 1
label info
u32 0x00ca7ca7
u32 7
u32 4
u32 3
str |xist|
str |test|
str |py|
{slices}ref data
u32 0
pop
""")
    data = assemble(bba.read_text())
    info = dump_chip_info(data)
    assert info["magic"] == "0xca7ca7"
    assert (info["version"], info["width"], info["height"]) == (7, 4, 3)
    assert (info["uarch"], info["name"], info["generator"]) == ("xist", "test", "py")
    assert info["tile_insts"] == 1 and info["packages"] == 1


def test_real_udev_grid():
    from xist.device import parse_udev_report
    p = Path(__file__).resolve().parents[1] / "xist/testdata/SL2-12E-8F256C_udev.txt"
    rep = parse_udev_report(p.read_text())
    assert (rep.rows, rep.cols) == (40, 51)
    assert rep.tile_type_counts()["PLC"] == 1504
    assert rep.counts["SLICE"] == 6016


def test_lut_maps():
    from xist.lutmap import load
    base = Path(__file__).resolve().parents[1] / "xist/testdata"
    for s in "ABCD":
        cal = load(base / f"SL2-12E_lut_{s}.json")
        assert len(cal.slot().minterms) == 16
        assert len(set(cal.slot().minterms)) == 16


def test_package_mapping():
    from xist.device import parse_pkg_report
    from xist.chipdb import pad_location
    p = Path(__file__).resolve().parents[1] / "xist/testdata/SL2-12E-8F256C_pkg.txt"
    pins = parse_pkg_report(p.read_text())
    assert len(pins) == 196
    assert all(pad_location(x["tile"]) for x in pins)
    assert pad_location("PL12C") == (12, 0, "C")
    assert pad_location("PB48D") == (39, 48, "D")


def test_slot_for_other_tiles():
    from xist.lutmap import load, slot_for
    base = Path(__file__).resolve().parents[1] / "xist/testdata"
    cal = load(base / "SL2-12E_lut_A.json")
    s = slot_for(cal, 10, 10, {10: 1888})
    assert s.minterms == tuple(cal.minterms[i] for i in range(16))
    # C11 is 48 bits to the left (measured independently)
    s11 = slot_for(cal, 10, 11, {11: 1840})
    assert s11.minterms[0] == (288, 1872)


def test_real_lut_codec():
    from xist.lutmap import load, slot_for
    base = Path(__file__).resolve().parents[1] / "xist/testdata"
    cal = load(base / "SL2-12E_lut_A.json")
    slot = slot_for(cal, 10, 10, {10: 1888})
    bs = make_bs(rows=400, cols=3000)
    for init in (0x0001, 0x6996, 0xC0C0, 0xFFFF):
        encode_lut_init(bs, init, slot)
        assert decode_lut_init(bs, slot) == init
    # all 16 fuses are inside the bitmap and distinct
    coords = [(r, c) for r, c, _ in slot.coords()]
    assert len(set(coords)) == 16
    assert all(0 <= r < 400 and 0 <= c < 3000 for r, c in coords)


def test_lut_position_samples():
    import json
    base = Path(__file__).resolve().parents[1] / "xist/testdata"
    d = json.loads((base / "SL2-12E_lut_samples.json").read_text())
    assert set(d["samples"]) == {"R10C10D", "R20C10D", "R10C19D", "R20C19D"}
    for cal in d["samples"].values():
        coords = [tuple(v) for v in cal["minterms"].values()]
        assert len(set(coords)) == 16


def test_nextpnr_json_convert(tmp_path):
    from xist.nextpnr_json import convert
    import json
    base = Path(__file__).resolve().parents[1] / "xist/testdata"
    data = json.loads((base / "simple_yosys.json").read_text())
    top = convert(data["modules"]["top"])
    types = sorted(c["type"] for c in top["cells"].values())
    assert types == ["DFF", "LUT4", "PIO", "PIO", "PIO", "PIO"]
    for c in top["cells"].values():
        if c["type"] == "LUT4":
            assert set(c["connections"]) <= {"I[0]", "I[1]", "I[2]", "I[3]", "F"}
            assert len(c["parameters"]["INIT"]) == 16


def test_lut_grid4_samples():
    import json
    p = Path(__file__).resolve().parents[1] / "xist/testdata/SL2-12E_lut_grid4.json"
    d = json.loads(p.read_text())
    samples = {k: v for k, v in d.items() if k.startswith("R")}
    assert len(samples) == 16
    assert all("error" not in v for v in samples.values())


def test_xpn_unquoted_signal(tmp_path):
    from xist.xpn import parse_xpn
    p = tmp_path / "u.xpn"
    p.write_text('''design t
{
   signal a_c
   {
      signal-pins
         ("a", PADDI),
         (SLICE_0, C0);
      route
         R39C19_JPADDIA_PIO.R39C19_JDIA,
   }
}
''')
    sigs = parse_xpn(p)
    assert list(sigs) == ["a_c"]
    assert sigs["a_c"].route == [("R39C19_JDIA", "R39C19_JPADDIA_PIO")]


def test_corpus_designs():
    from xist.corpus import DESIGNS
    assert len(DESIGNS) >= 8
    assert "lut_chain" in DESIGNS and "io_pass" in DESIGNS


def test_xpn_last_route_entry_with_semicolon(tmp_path):
    from xist.xpn import parse_xpn
    p = tmp_path / "last.xpn"
    p.write_text('''signal o_c
{
   signal-pins
      (SLICE_0, F0),
      ("o", PADDO);
   route
      R39C19_JPADDOC.R39C19_PADDOC_PIO,
      R38C19_V02N0701.R38C19_JA2;
}
''')
    sigs = parse_xpn(p)
    assert sigs["o_c"].route == [
        ("R39C19_PADDOC_PIO", "R39C19_JPADDOC"),
        ("R38C19_JA2", "R38C19_V02N0701"),
    ]
