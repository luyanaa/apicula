"""Python implementation of nextpnr's ``bbasm`` (Binary Blob Assembler).

nextpnr's chipdb is a position-independent binary blob produced from the text
``.bba`` emitted by :mod:`xist.chipdb`.  Upstream ships a small C++ tool, but the
format is simple enough to assemble in Python; that keeps the XIST database
build self-contained.

Reference: nextpnr/bba/README.md
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path


class BbaError(Exception):
    pass


def assemble(text: str) -> bytes:
    streams: list[tuple[str, bytearray]] = []
    cur: bytearray | None = None
    labels: dict[str, tuple[int, int]] = {}
    refs: list[tuple[int, int, str]] = []
    strpool: list[bytes] = []
    str_idx: dict[bytes, int] = {}
    str_refs: list[tuple[int, int, int]] = []
    depth = 0

    def emit(data: bytes) -> None:
        nonlocal cur
        if cur is None:
            raise BbaError("data outside push/pop")
        cur += data

    def emit_ref(name: str) -> None:
        nonlocal cur
        if cur is None:
            raise BbaError("ref outside push/pop")
        refs.append((len(streams) - 1, len(cur), name))
        cur += b"\x00\x00\x00\x00"

    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        op = parts[0]
        rest = parts[1] if len(parts) > 1 else ""
        if op == "pre" or op == "post":
            continue
        if op == "push":
            depth += 1
            cur = bytearray()
            streams.append((rest.strip(), cur))
        elif op == "pop":
            depth -= 1
            if depth < 0:
                raise BbaError(f"line {lineno}: pop without push")
            cur = streams[-1][1] if streams else None
        elif op == "label":
            if cur is None:
                raise BbaError(f"line {lineno}: label outside push/pop")
            labels[rest.split()[0]] = (len(streams) - 1, len(cur))
        elif op == "ref":
            emit_ref(rest.split()[0])
        elif op == "slice":
            f = rest.split()
            emit_ref(f[0])
            emit(struct.pack("<I", int(f[1])))
        elif op == "u8":
            emit(struct.pack("<B", int(rest.split()[0], 0) & 0xFF))
        elif op == "u16":
            emit(struct.pack("<H", int(rest.split()[0], 0) & 0xFFFF))
        elif op == "u32":
            emit(struct.pack("<I", int(rest.split()[0], 0) & 0xFFFFFFFF))
        elif op == "str":
            body = rest.strip()
            if body and body[0] in "\"'|":
                q = body[0]
                end = body.index(q, 1)
                s = body[1:end].encode()
            else:
                s = body.split()[0].encode()
            idx = str_idx.setdefault(s, len(strpool))
            if idx == len(strpool):
                strpool.append(s)
            if cur is None:
                raise BbaError(f"line {lineno}: str outside push/pop")
            str_refs.append((len(streams) - 1, len(cur), idx))
            cur += b"\x00\x00\x00\x00"
        else:
            raise BbaError(f"line {lineno}: unknown op {op!r}")
    if depth != 0:
        raise BbaError("unbalanced push/pop")
    if not streams:
        raise BbaError("no push block")

    # lay out streams in order, then the string pool
    bases = []
    off = 0
    for _, buf in streams:
        bases.append(off)
        off += len(buf)
    str_base = off
    for s in strpool:
        off += len(s) + 1
    out = bytearray(off)

    for (_, buf), base in zip(streams, bases):
        out[base:base + len(buf)] = buf

    for sidx, roff, target in refs:
        if target not in labels:
            raise BbaError(f"undefined label {target!r}")
        tsidx, toff = labels[target]
        ref_abs = bases[sidx] + roff
        val = (bases[tsidx] + toff) - ref_abs
        out[ref_abs:ref_abs + 4] = struct.pack("<i", val)

    # write strings and resolve their refs
    positions = []
    p = str_base
    for s in strpool:
        positions.append(p)
        out[p:p + len(s)] = s
        p += len(s) + 1
    for sidx, roff, idx in str_refs:
        ref_abs = bases[sidx] + roff
        out[ref_abs:ref_abs + 4] = struct.pack("<i", positions[idx] - ref_abs)

    return bytes(out)


def dump_chip_info(data: bytes) -> dict:
    """Decode the chip_info header of a nextpnr chipdb blob (sanity check).

    The blob starts with a relative reference to ``chip_info`` (see the
    ``push chipdb_blob; ref chip_info; ...`` prologue in chip.py).
    """
    info_off = struct.unpack_from("<i", data, 0)[0]
    magic, version, width, height = struct.unpack_from("<IIII", data, info_off)
    return dict(magic=hex(magic), version=version, width=width, height=height,
                info_offset=info_off, size=len(data))


def main(argv=None) -> int:
    ap_argv = sys.argv[1:] if argv is None else argv
    if len(ap_argv) < 2:
        print("usage: python -m xist.bbasm input.bba output.bin")
        return 2
    data = assemble(Path(ap_argv[0]).read_text())
    Path(ap_argv[1]).write_bytes(data)
    print(f"{ap_argv[1]}: {len(data)} bytes")
    print(dump_chip_info(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
