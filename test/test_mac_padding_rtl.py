#
# This file is part of LiteEth.
#
# Copyright (c) 2026 Oliver Keller <oliver.keller@pm.me>
# SPDX-License-Identifier: BSD-2-Clause

import shutil
import unittest

from liteeth.common import eth_min_frame_length, eth_fcs_length
from liteeth.mac.padding import LiteEthMACPaddingChecker
from test.rtl_helpers import rtl_available, run_rtl

simulator = "iverilog" if rtl_available else ("verilator" if shutil.which("verilator") else None)


@unittest.skipUnless(simulator, "Icarus Verilog or Verilator required")
class TestMACPaddingCheckerRTL(unittest.TestCase):
    # Generated Verilog sizes expressions differently from the Migen simulator: check the runt
    # flag on the RTL, around full-word lengths and powers of two.
    lengths = [1, 59, 60, 61, 63, 64, 65, 68, 100, 1514, 1518, 2047, 2048, 2049, 2108, 4096, 9000, 59]

    def test_runt_flag(self):
        packet_min_length = eth_min_frame_length - eth_fcs_length
        for dw in [8, 32, 64]:
            with self.subTest(dw=dw):
                nb  = dw//8
                dut = LiteEthMACPaddingChecker(dw, packet_min_length)
                frames = "\n".join(f"    send({l}, {int(l < packet_min_length)});" for l in self.lengths)
                run_rtl(dut, {
                    "sink_valid": dut.sink.valid, "sink_last": dut.sink.last,
                    "sink_be": dut.sink.be, "source_ready": dut.source.ready,
                }, {
                    "source_error": dut.source.error,
                }, f'''
task send(input integer length, input integer runt);
    integer i, words;
    begin
        words = (length + {nb} - 1)/{nb};
        for (i = 0; i < words; i = i + 1) begin
            @(negedge sys_clk);
            sink_valid = 1;
            sink_last  = (i == words - 1);
            sink_be    = (1 << ((i == words - 1) ? length - i*{nb} : {nb})) - 1;
            #1;
            if (sink_last && ((source_error != 0) != runt))
                $fatal(1, "length %0d: runt flag %0d, expected %0d", length, source_error != 0, runt);
        end
        @(negedge sys_clk);
        sink_valid = 0;
        sink_last  = 0;
    end
endtask
initial begin
    source_ready = 1;
    @(negedge sys_clk);
    sys_rst = 0;
{frames}
    $finish;
end
''', cycles=100000, simulator=simulator)
