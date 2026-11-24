"""Generate a small routing-golden corpus with the vendor flow.

Each design exercises a different wire family (LUT chains, mux trees, carry
chains, fanout, IO buffers, registers).  The routed XPN dumps are the input for
building the nextpnr routing graph.
"""

from __future__ import annotations

import sys
from pathlib import Path

from . import hqfpga

DESIGNS: dict[str, str] = {
    "lut_chain": """module top(input a, output o);
  wire [7:0] w;
  assign w[0] = a;
  genvar i;
  generate for (i = 0; i < 7; i = i + 1) begin : g
    assign w[i+1] = ~(w[i] ^ a);
  end endgenerate
  assign o = w[7];
endmodule
""",
    "mux4": """module top(input [1:0] s, input [3:0] d, output o);
  assign o = d[s];
endmodule
""",
    "adder4": """module top(input [3:0] a, b, output [4:0] o);
  assign o = a + b;
endmodule
""",
    "shift8": """module top(input clk, input a, output [7:0] q);
  reg [7:0] r;
  always @(posedge clk) r <= {r[6:0], a};
  assign q = r;
endmodule
""",
    "counter8": """module top(input clk, input rst, output [7:0] q);
  reg [7:0] r;
  always @(posedge clk) if (rst) r <= 0; else r <= r + 1;
  assign q = r;
endmodule
""",
    "io_pass": """module top(input a, b, c, d, output o1, o2, o3, o4);
  assign o1 = a & b;
  assign o2 = b | c;
  assign o3 = c ^ d;
  assign o4 = ~(a & d);
endmodule
""",
    "fanout8": """module top(input a, output [7:0] o);
  assign o = {8{a}};
endmodule
""",
    "xbar4": """module top(input [3:0] a, output [3:0] o);
  assign o[0] = a[1] & a[2];
  assign o[1] = a[0] | a[3];
  assign o[2] = a[2] ^ a[0];
  assign o[3] = ~(a[1] | a[3]);
endmodule
""",
    "dff_chain": """module top(input clk, input a, output q);
  reg [7:0] r;
  always @(posedge clk) r <= {r[6:0], a};
  assign q = r[7];
endmodule
""",
    "wide_eq": """module top(input [7:0] a, b, output o);
  assign o = (a == b);
endmodule
""",
}


def generate(part: str, workdir: str | Path, designs: list[str] | None = None,
             timeout: int = 900) -> list[Path]:
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    for name in (designs or list(DESIGNS)):
        if name not in DESIGNS:
            raise KeyError(f"unknown design {name!r}")
        (workdir / f"{name}.v").write_text(DESIGNS[name])
        tcl = "\n".join([
            f"dv.setup {_family(part)} {part}",
            f"design.analyze {{{name}.v}}",
            "design.rtlsyn -top top",
            "design.map", "design.pack", "design.place -seed 1", "design.route",
            f"xpn.write {name}.xpn",
            f"nl.write {name}_phy.v -phy -loc",
            "exit", ""])
        proc = hqfpga.run_script(tcl, str(workdir), log=f"{name}.log",
                                 timeout=timeout)
        if proc.returncode != 0:
            print(f"{name}: FAILED rc={proc.returncode}")
            continue
        out.append(workdir / f"{name}.xpn")
        print(f"{name}: ok")
    return out


def _family(part: str) -> str:
    from .device import family_for
    return family_for(part)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) < 2:
        print("usage: python -m xist.corpus PART OUTDIR [design ...]")
        return 2
    paths = generate(argv[0], argv[1], argv[2:] or None)
    print(f"{len(paths)} XPN files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
