"""XIST (HqFpga) ASCII bitstream reader/writer and differential helpers.

The vendor .bit format (HqFpga-XIST V3.1.0, SEALION SL2-12E) is a text header
followed by a ``Rows x Cols`` bitmap, one ASCII character per configuration bit,
row major.  The last bitmap line carries a 16-bit trailer:
``CRC-16/0x8005`` (non-reflected, init 0) over the bit-packed bitmap XOR
``0x1FE2``.  See ``doc/xist.md`` for the evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# Within-tile LUT INIT layout, calibrated for SEALION SL2-12E slice A (F0).
# Coordinates are relative to the parsed bitmap (not raw file lines).
ROW_PITCH = 29
# F0: upper half (minterms 8..15) at 29R-3, lower half (0..7) at 29R-2.
# F1: upper half at 29R-4, lower half at 29R-5 (one 2-row block above F0).
LUT_SLOT_ROW_OFFSETS = {
    "F0": (-3, -2),
    "F1": (-5, -4),
}
# minterm j (0..7) -> column offset inside the 8-column block.
# For slice A.  Other slices have their own permutation; calibrate per device.
PERM_SLICE_A = (0, 1, 3, 2, 4, 5, 7, 6)


def _make_crc8005_table() -> list[int]:
    table = []
    for i in range(256):
        c = i << 8
        for _ in range(8):
            c = ((c << 1) ^ 0x8005) & 0xFFFF if c & 0x8000 else (c << 1) & 0xFFFF
        table.append(c)
    return table


_CRC8005_TABLE = _make_crc8005_table()


@dataclass
class Bitstream:
    header_lines: list[str] = field(default_factory=list)
    header: dict[str, str] = field(default_factory=dict)
    pre_block: list[str] = field(default_factory=list)
    rows: list[str] = field(default_factory=list)
    footer_block: list[str] = field(default_factory=list)
    extra16: str = ""

    @classmethod
    def read(cls, path: str) -> "Bitstream":
        raw = open(path, "r", encoding="ascii", errors="replace").read().split("\n")
        header_lines: list[str] = []
        header: dict[str, str] = {}
        i = 0
        while i < len(raw) and raw[i].strip() != "":
            line = raw[i]
            header_lines.append(line)
            if ":" in line:
                k, v = line.split(":", 1)
                header[k.strip()] = v.strip()
            i += 1
        nrows = int(header.get("Rows", "0"))
        ncols = int(header.get("Cols", "0"))
        while i < len(raw) and raw[i].strip() == "":
            i += 1
        pre: list[str] = []
        while i < len(raw) and (not raw[i].strip() or len(raw[i]) < ncols):
            pre.append(raw[i])
            i += 1
        # drop the blank line(s) we just consumed into pre_block; they are
        # re-emitted by write()
        while pre and pre[-1].strip() == "":
            pre.pop()
        rows: list[str] = []
        extra16 = ""
        while i < len(raw) and len(rows) < nrows:
            line = raw[i]
            i += 1
            if len(line) >= ncols and set(line[:ncols]) <= {"0", "1"}:
                rows.append(line[:ncols])
                if len(line) > ncols:
                    extra16 = line[ncols:]
            elif line.strip() == "":
                continue
            else:
                pre.append(line)
        footer: list[str] = []
        while i < len(raw):
            if raw[i].strip():
                footer.append(raw[i])
            i += 1
        return cls(header_lines, header, pre, rows, footer, extra16)

    @property
    def nrows(self) -> int:
        return len(self.rows)

    @property
    def ncols(self) -> int:
        return len(self.rows[0]) if self.rows else 0

    def bit(self, r: int, c: int) -> int:
        return 1 if self.rows[r][c] == "1" else 0

    def write(self, path: str) -> None:
        with open(path, "w", encoding="ascii", newline="\n") as f:
            for line in self.header_lines:
                f.write(line + "\n")
            f.write("\n")
            for line in self.pre_block:
                f.write(line + "\n")
            for idx, line in enumerate(self.rows):
                f.write(line)
                if idx == len(self.rows) - 1 and self.extra16:
                    f.write(self.extra16)
                f.write("\n")
            for line in self.footer_block:
                f.write(line + "\n")

    def packed_bytes(self) -> bytes:
        """Bit-packed configuration data (MSB first), all rows, no trailer."""
        out = bytearray()
        for row in self.rows:
            n = len(row)
            for i in range(0, n, 8):
                v = 0
                for ch in row[i:i + 8]:
                    v = (v << 1) | (1 if ch == "1" else 0)
                out.append(v)
        return bytes(out)

    @staticmethod
    def crc16(data: bytes, init: int = 0) -> int:
        """CRC-16, polynomial 0x8005, non-reflected (MSB first)."""
        table = _CRC8005_TABLE
        crc = init
        for b in data:
            crc = ((crc << 8) & 0xFFFF) ^ table[((crc >> 8) ^ b) & 0xFF]
        return crc

    def checksum(self) -> int:
        """The 16-bit trailer: CRC-16/0x8005 of the packed bitmap XOR 0x1FE2."""
        return self.crc16(self.packed_bytes()) ^ 0x1FE2

    def verify_checksum(self) -> bool:
        return self.extra16 == format(self.checksum(), "016b")

    def update_checksum(self) -> None:
        self.extra16 = format(self.checksum(), "016b")

    def to_bytes(self) -> bytes:
        import io

        buf = io.StringIO()
        for line in self.header_lines:
            buf.write(line + "\n")
        buf.write("\n")
        for line in self.pre_block:
            buf.write(line + "\n")
        for idx, line in enumerate(self.rows):
            buf.write(line)
            if idx == len(self.rows) - 1 and self.extra16:
                buf.write(self.extra16)
            buf.write("\n")
        for line in self.footer_block:
            buf.write(line + "\n")
        return buf.getvalue().encode("ascii")


@dataclass(frozen=True)
class LutSlot:
    """Fuse geometry for one LUT4 INIT.

    ``minterms[j]`` is the (bitmap row, bitmap column) storing minterm *j*.
    The layout is not uniform across slices: some use two 8-bit rows, others
    interleave even/odd minterms between rows, so the full 16-entry map is kept.
    """

    minterms: tuple[tuple[int, int], ...]

    def coords(self) -> list[tuple[int, int, int]]:
        return [(r, c, j) for j, (r, c) in enumerate(self.minterms)]

    @classmethod
    def byte_split(cls, tile_row: int, slot: str, block_col: int,
                   perm: tuple[int, ...] = PERM_SLICE_A) -> "LutSlot":
        off = LUT_SLOT_ROW_OFFSETS[slot]
        upper = ROW_PITCH * tile_row + off[0]
        lower = ROW_PITCH * tile_row + off[1]
        return cls(tuple(
            (lower, block_col + perm[j]) if j < 8
            else (upper, block_col + perm[j - 8]) for j in range(16)))


def encode_lut_init(bs: Bitstream, init: int, slot: LutSlot) -> None:
    for br, bc, j in slot.coords():
        val = "0" if (init >> j) & 1 else "1"  # stored complemented
        bs.rows[br] = bs.rows[br][:bc] + val + bs.rows[br][bc + 1:]


def decode_lut_init(bs: Bitstream, slot: LutSlot) -> int:
    init = 0
    for br, bc, j in slot.coords():
        if bs.rows[br][bc] == "0":
            init |= 1 << j
    return init


def diff_positions(a: Bitstream, b: Bitstream,
                   row_range: tuple[int, int] | None = None) -> dict[int, list[int]]:
    """Return {row: [cols]} for differing bitmap bits."""
    if a.nrows != b.nrows or a.ncols != b.ncols:
        raise ValueError(f"size mismatch {a.nrows}x{a.ncols} vs {b.nrows}x{b.ncols}")
    r0, r1 = row_range or (0, a.nrows)
    out: dict[int, list[int]] = {}
    for r in range(max(0, r0), min(r1, a.nrows)):
        if a.rows[r] == b.rows[r]:
            continue
        cols = [c for c in range(a.ncols) if a.rows[r][c] != b.rows[r][c]]
        if cols:
            out[r] = cols
    return out
