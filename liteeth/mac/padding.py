#
# This file is part of LiteEth.
#
# Copyright (c) 2015-2021 Florent Kermarrec <florent@enjoy-digital.fr>
# Copyright (c) 2021 David Sawatzke <d-git@sawatzke.dev>
# Copyright (c) 2015 Sebastien Bourdeauducq <sb@m-labs.hk>
# Copyright (c) 2018 whitequark <whitequark@whitequark.org>
# Copyright (c) 2025 Fin Maaß <f.maass@vogl-electronic.com>
# SPDX-License-Identifier: BSD-2-Clause

import math

from liteeth.common import *

# MAC Padding Inserter -----------------------------------------------------------------------------

class LiteEthMACPaddingInserter(Module):
    def __init__(self, dw, padding):
        assert dw in [8, 16, 32, 64, 128, 256, 512]
        self.sink   = sink   = stream.Endpoint(eth_phy_description(dw))
        self.source = source = stream.Endpoint(eth_phy_description(dw))

        # # #

        padding_limit = math.ceil(padding/(dw/8))-1
        be            = (1 << ((padding - 1) % (dw//8) + 1)) - 1

        counter      = Signal(16)
        counter_done = Signal()
        self.comb += counter_done.eq(counter >= padding_limit)

        self.submodules.fsm = fsm = FSM(reset_state="IDLE")
        fsm.act("IDLE",
            sink.connect(source),
            If(sink.last,
                # Padding bytes are zero, including lanes in a partially occupied word.
                *[If(~sink.be[i], source.data[8*i:8*(i+1)].eq(0)) for i in range(dw//8)],
                If(~counter_done,
                    source.last.eq(0),
                    source.be.eq((1 << (dw//8)) - 1),
                ).Elif((counter == padding_limit) & (be > sink.be),
                    source.be.eq(be),
                ),
            ),
            If(source.valid & source.ready,
                NextValue(counter, counter + 1),
                If(sink.last,
                    If(~counter_done,
                        NextState("PADDING"),
                    ).Else(
                        NextValue(counter, 0),
                    )
                )
            )
        )
        fsm.act("PADDING",
            source.valid.eq(1),
            source.be.eq((1 << (dw//8)) - 1),
            If(counter_done,
                source.be.eq(be),
                source.last.eq(1)),
            source.data.eq(0),
            If(source.valid & source.ready,
                NextValue(counter, counter + 1),
                If(counter_done,
                    NextValue(counter, 0),
                    NextState("IDLE")
                )
            )
        )


# MAC Padding Checker ------------------------------------------------------------------------------

class LiteEthMACPaddingChecker(Module):
    def __init__(self, dw, packet_min_length, eth_mtu=eth_mtu_default):
        self.sink   = sink   = stream.Endpoint(eth_phy_description(dw))
        self.source = source = stream.Endpoint(eth_phy_description(dw))

        # # #

        # drop the packet when
        # payload size < minimum ethernet payload size

        # Only lengths below packet_min_length matter: the count saturates there, so that it can't
        # wrap whatever the frame length (eth_mtu is no longer needed and only kept for
        # compatibility). The sum gets its own signal, wide enough for a saturated count plus a
        # full word: in Verilog, an inline sum would be truncated to the width of the comparison.
        length      = Signal(max=packet_min_length + dw//8)
        length_inc  = Signal(bits_for(dw//8))
        length_next = Signal(max=packet_min_length + 2*(dw//8))

        # Count valid bytes.
        self.comb += [
            length_inc.eq(stream.byte_count(sink.be)),
            length_next.eq(length + length_inc),
        ]

        self.sync += [
            If(sink.valid & sink.ready,
                If(sink.last,
                    length.eq(0),
                ).Elif(length < packet_min_length,
                    length.eq(length_next)
                )
            )
        ]

        self.comb += [
            sink.connect(source, omit={"error"}),

            If(sink.valid & sink.last & (length_next < packet_min_length),
                source.error.eq(Replicate(1, dw//8)),
            ).Else(
                source.error.eq(sink.error),
            )
        ]
