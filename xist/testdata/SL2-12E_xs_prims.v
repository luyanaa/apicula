// Blackbox declarations for the XiST (SEALION) IOLOGIC DDR primitives.
//
// The vendor RTL analyzer accepts these names (not Gowin's ODDR/IDDR); the
// modules themselves are described by common/sim/verilog/XIST/sealion/*.v.
// Yosys needs a blackbox declaration to infer ports and parameters, and the
// `xist/nextpnr_json.py` converter maps them onto the XIST ODDR/IDDR cells.
//
// Ports are taken verbatim from the vendor simulation models:
//   xsODDR    (D0, D1, RST, SCLK, Q)
//   xsODDRX2  (D0, D1, D2, D3, RST, ECLK, SCLK, Q)
//   xsIDDRX1  (D, SCLK, RST, Q0, Q1)

(* blackbox *)
module xsODDR (D0, D1, RST, SCLK, Q);
  parameter GSR = "ENABLED";
  input D0, D1, RST, SCLK;
  output Q;
endmodule

(* blackbox *)
module xsODDRX2 (D0, D1, D2, D3, RST, ECLK, SCLK, Q);
  parameter GSR = "ENABLED";
  input D0, D1, D2, D3, RST, ECLK, SCLK;
  output Q;
endmodule

(* blackbox *)
module xsIDDRX1 (D, SCLK, RST, Q0, Q1);
  parameter GSR = "ENABLED";
  input D, SCLK, RST;
  output Q0, Q1;
endmodule

(* blackbox *)
module xsIODLY (A, Z);
  parameter DEL_MODE = "USER_DEFINED";
  parameter DEL_VALUE = "DELAY0";
  input A;
  output Z;
endmodule
