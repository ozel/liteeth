// This file is part of LiteEth.
//
// Copyright (c) 2026 Oliver Keller <oliver.keller@pm.me>
// SPDX-License-Identifier: BSD-2-Clause
//
// Simulation models of the I/O primitives used by the ECP5 RGMII PHY. Delays are not modeled.

`timescale 1ns / 1ps

module DELAYG #(
    parameter DEL_MODE  = "SCLK_ALIGNED",
    parameter DEL_VALUE = 0
) (
    input  A,
    output Z
);
    assign Z = A;
endmodule

// DDR I/Os, as lowered by LiteX's simulation platform (DDRInput/DDROutput), with ECP5 IDDRX1F/
// ODDRX1F pairing: o1/o2 hold the samples of a rising edge and of the falling edge that follows it,
// both updated on the next rising edge; o drives i1 while clk is high, then i2.

module DDR_INPUT (
    output reg o1,
    output reg o2,
    input      i,
    input      clk
);
    reg rise;
    reg fall;
    always @(posedge clk) rise <= i;
    always @(negedge clk) fall <= i;
    always @(posedge clk) begin
        o1 <= rise;
        o2 <= fall;
    end
endmodule

module DDR_OUTPUT (
    input  i1,
    input  i2,
    output o,
    input  clk
);
    reg d1;
    reg d2;
    always @(posedge clk) begin
        d1 <= i1;
        d2 <= i2;
    end
    assign o = clk ? d1 : d2;
endmodule
