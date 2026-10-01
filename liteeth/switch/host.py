#
# This file is part of LiteEth.
#
# Copyright (c) 2026 Oliver Keller <oliver.keller@pm.me>
# SPDX-License-Identifier: BSD-2-Clause

from litex.gen import *

from liteeth.common import *
from liteeth.mac.common import LiteEthMACCrossbar, LiteEthMACPacketizer, LiteEthMACDepacketizer
from liteeth.core.arp  import LiteEthARP
from liteeth.core.ip   import LiteEthIP
from liteeth.core.udp  import LiteEthUDP
from liteeth.core.icmp import LiteEthICMP

# Switch Host MAC ----------------------------------------------------------------------------------

class LiteEthSwitchHostMAC(LiteXModule):
    """MAC layer of a host living on a switch port.

    Switch ports already carry checked frames in the ``sys`` domain, so the host only needs the
    MAC header (de)packetization and the EtherType crossbar of :class:`LiteEthMAC`: no PHY, MAC core
    or clock domain crossing.
    """
    def __init__(self, dw=8):
        self.sink   = stream.Endpoint(eth_phy_description(dw)) # Frames from the switch.
        self.source = stream.Endpoint(eth_phy_description(dw)) # Frames to the switch.

        # # #

        self.crossbar     = LiteEthMACCrossbar(dw)
        self.packetizer   = LiteEthMACPacketizer(dw)
        self.depacketizer = LiteEthMACDepacketizer(dw)
        self.comb += [
            self.crossbar.master.source.connect(self.packetizer.sink),
            self.packetizer.source.connect(self.source),
            self.sink.connect(self.depacketizer.sink),
            self.depacketizer.source.connect(self.crossbar.master.sink),
        ]

# Switch UDP/IP Core -------------------------------------------------------------------------------

class LiteEthSwitchUDPIPCore(LiteXModule):
    """LiteEth ARP/IP/ICMP/UDP stack attached to a switch port (see :class:`LiteEthUDPIPCore`)."""
    def __init__(self, mac_address, ip_address, clk_freq, dw=8,
        arp_entries       = 1,
        with_icmp         = True,
        icmp_fifo_depth   = 128,
        with_ip_broadcast = True,
        eth_mtu           = eth_mtu_default,
        ):
        ip_address = convert_ip(ip_address)

        # MAC.
        self.mac    = LiteEthSwitchHostMAC(dw)
        self.sink   = self.mac.sink
        self.source = self.mac.source

        # ARP.
        self.arp = LiteEthARP(
            mac         = self.mac,
            mac_address = mac_address,
            ip_address  = ip_address,
            clk_freq    = clk_freq,
            entries     = arp_entries,
            dw          = dw,
        )

        # IP.
        self.ip = LiteEthIP(
            mac            = self.mac,
            mac_address    = mac_address,
            ip_address     = ip_address,
            arp_table      = self.arp.table,
            with_broadcast = with_ip_broadcast,
            dw             = dw,
        )

        # ICMP (Optional).
        if with_icmp:
            self.icmp = LiteEthICMP(
                ip         = self.ip,
                ip_address = ip_address,
                dw         = dw,
                fifo_depth = icmp_fifo_depth,
            )

        # UDP.
        self.udp = LiteEthUDP(
            ip         = self.ip,
            ip_address = ip_address,
            dw         = dw,
            eth_mtu    = eth_mtu,
        )
