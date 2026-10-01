#
# This file is part of LiteEth.
#
# Copyright (c) 2026 Oliver Keller <oliver.keller@pm.me>
# SPDX-License-Identifier: BSD-2-Clause

from functools import reduce
from operator import and_, or_

from litex.gen import *

from litex.soc.interconnect import stream
from litex.soc.interconnect.csr import *

from liteeth.common import *
from liteeth.fifo import PacketDropFIFO
from liteeth.mac.core import LiteEthMACCore
from liteeth.switch.table import LiteEthSwitchMACTable, switch_lookup_layout, switch_result_layout

# Helpers ------------------------------------------------------------------------------------------

def _counter(module, csr, event):
    module.sync += If(event, csr.status.eq(csr.status + 1))

# Switch Port --------------------------------------------------------------------------------------

class LiteEthSwitchPort:
    """Frame interface of a switch port, in the ``sys`` clock domain.

    Frames carry everything from the destination MAC address through the payload, without
    preamble or FCS (the format of :class:`LiteEthMACCore` endpoints).

    - ``sink``  : frames received on the port, entering the switch.
    - ``source``: frames leaving the switch, to transmit on the port.
    """
    def __init__(self, dw):
        self.sink   = stream.Endpoint(eth_phy_description(dw))
        self.source = stream.Endpoint(eth_phy_description(dw))

# Switch PHY Port ----------------------------------------------------------------------------------

class LiteEthSwitchPHYPort(LiteXModule):
    """MAC core (preamble/FCS, padding, IFG, CDC and width conversion) between a PHY and a port.

    ``cd`` renames the core's ``eth_rx``/``eth_tx`` clock domains to ``<cd>_rx``/``<cd>_tx``, so
    several PHYs can coexist; rename the PHY's own clock domains identically. Clock domain
    crossings are buffered by default (as in :class:`LiteEthIPCore`), easing timing in the PHY
    domains; ``with_sys_datapath=True`` further moves preamble/FCS/padding processing to ``sys``.
    """
    def __init__(self, phy, dw, cd=None, tx_cdc_buffered=True, rx_cdc_buffered=True, **kwargs):
        core = LiteEthMACCore(phy=phy, dw=dw,
            tx_cdc_buffered = tx_cdc_buffered,
            rx_cdc_buffered = rx_cdc_buffered,
            **kwargs)
        if cd is not None:
            core = ClockDomainsRenamer({"eth_rx": f"{cd}_rx", "eth_tx": f"{cd}_tx"})(core)
        self.core   = core
        self.sink   = core.sink   # Frames to transmit.
        self.source = core.source # Frames received.

# Switch Ingress -----------------------------------------------------------------------------------

class LiteEthSwitchIngress(LiteXModule):
    """Store-and-forward ingress of a port.

    Complete frames are buffered in a :class:`PacketDropFIFO` together with their destination and
    source addresses, captured on the fly. Frames with errors, runts (shorter than a MAC header) and
    frames overflowing the buffer are dropped before they can be forwarded, and only good frames
    are learned from.

    For the frame at the head of the buffer, the ingress queries the MAC table, derives the set of
    egress ports, requests them all from the allocator and, once granted, streams the frame to
    them in lockstep.
    """
    def __init__(self, dw, port, nports, buffer_depth, param_depth=16):
        nb       = dw//8
        all_mask = 2**nports - 1

        self.sink   = sink   = stream.Endpoint(eth_phy_description(dw))
        self.source = source = stream.Endpoint(eth_phy_description(dw))

        # MAC Table interface.
        self.lookup = lookup = stream.Endpoint(switch_lookup_layout())
        self.result = result = stream.Endpoint(switch_result_layout(nports))

        # Allocator interface.
        self.request = Signal()
        self.mask    = Signal(nports)
        self.grant   = Signal()
        self.release = Signal()

        # Port-based isolation: egress ports this port may forward to.
        self.forward_mask = Signal(nports, reset=all_mask)

        # Statistics events (single-cycle pulses).
        self.ev_rx_frame = Signal()
        self.ev_rx_drop  = Signal()
        self.ev_filtered = Signal()
        self.ev_flooded  = Signal()

        # # #

        # Header Capture.
        # ---------------
        # Byte 13 (end of the EtherType) is the last byte a frame needs to be forwarded.
        b13       = 13//nb
        beat      = Signal(max=b13 + 2)
        hdr       = Signal(96)
        hdr_next  = Signal(96)
        for k in range(12):
            b, lane = k//nb, k%nb
            self.comb += If(beat == b,
                hdr_next[8*k:8*(k + 1)].eq(sink.data[8*lane:8*(lane + 1)])
            ).Else(
                hdr_next[8*k:8*(k + 1)].eq(hdr[8*k:8*(k + 1)])
            )
        self.sync += If(sink.valid,
            hdr.eq(hdr_next),
            If(sink.last,
                beat.eq(0),
            ).Elif(beat != (b13 + 1),
                beat.eq(beat + 1),
            )
        )
        long_enough = Signal()
        has_error   = Signal()
        self.comb += [
            long_enough.eq((beat > b13) | ((beat == b13) & sink.be[13%nb])),
            has_error.eq((sink.error & sink.be) != 0),
        ]

        # Frame Buffer.
        # -------------
        fifo_description = stream.EndpointDescription(
            payload_layout = eth_phy_description(dw).payload_layout,
            param_layout   = [("dst_mac", 48), ("src_mac", 48)],
        )
        self.fifo = fifo = PacketDropFIFO(fifo_description,
            payload_depth = buffer_depth,
            param_depth   = param_depth,
        )
        self.comb += [
            sink.connect(fifo.sink),
            fifo.sink.dst_mac.eq(hdr_next[ 0:48]),
            fifo.sink.src_mac.eq(hdr_next[48:96]),
            fifo.discard.eq(sink.valid & (has_error | (sink.last & ~long_enough))),
            self.ev_rx_frame.eq(sink.valid & sink.last),
            self.ev_rx_drop.eq(fifo.drop),
        ]

        # Forwarding.
        # -----------
        head      = fifo.source
        mcast     = head.dst_mac[0] # Group bit: broadcast/multicast.
        others    = all_mask & ~(1 << port)
        hit_mask  = Signal(nports)
        new_mask  = Signal(nports)
        mask      = Signal(nports)
        flooded   = Signal()
        self.comb += [
            # Learn unicast sources only.
            lookup.dst_mac.eq(head.dst_mac),
            lookup.src_mac.eq(head.src_mac),
            lookup.learn.eq(~head.src_mac[0]),
            lookup.last.eq(1),
            # Unknown or group destinations are flooded; never send a frame back where it came from.
            hit_mask.eq(Cat(*[result.port == n for n in range(nports)])),
            If(mcast | ~result.hit,
                new_mask.eq(others),
            ).Else(
                new_mask.eq(hit_mask & others),
            ),
            self.mask.eq(mask),
        ]

        self.fsm = fsm = FSM(reset_state="IDLE")
        fsm.act("IDLE",
            If(head.valid,
                lookup.valid.eq(1),
                If(lookup.ready,
                    NextState("RESULT"),
                )
            )
        )
        fsm.act("RESULT",
            result.ready.eq(1),
            If(result.valid,
                NextValue(mask,    new_mask & self.forward_mask),
                NextValue(flooded, mcast | ~result.hit),
                NextState("ROUTE"),
            )
        )
        fsm.act("ROUTE",
            If(mask == 0,
                self.ev_filtered.eq(1),
                NextState("DROP"),
            ).Else(
                self.ev_flooded.eq(flooded),
                NextState("REQUEST"),
            )
        )
        fsm.act("REQUEST",
            self.request.eq(1),
            If(self.grant,
                NextState("FORWARD"),
            )
        )
        fsm.act("FORWARD",
            head.connect(source, omit={"dst_mac", "src_mac"}),
            If(head.valid & head.ready & head.last,
                self.release.eq(1),
                NextState("IDLE"),
            )
        )
        fsm.act("DROP",
            head.ready.eq(1),
            If(head.valid & head.last,
                NextState("IDLE"),
            )
        )

# Switch Allocator ---------------------------------------------------------------------------------

class LiteEthSwitchAllocator(LiteXModule):
    """Frame-level egress allocator.

    An ingress requests all its egress ports (``mask``) at once and is granted all of them
    atomically, holding them until its frame is done (``release``). Grants never hold-and-wait, so
    concurrent multicasts can't deadlock.

    Requests are served in round-robin order. The pointer only advances once the first requester in
    priority order has been served, and lower-priority requesters can't take egress ports it is
    waiting for: a flood waiting for busy ports can't be starved by unicast traffic. Other
    requests whose ports don't conflict proceed in parallel.
    """
    def __init__(self, nports):
        n = nports
        self.request = [Signal()  for _ in range(n)]
        self.mask    = [Signal(n) for _ in range(n)]
        self.grant   = [Signal()  for _ in range(n)]
        self.release = [Signal()  for _ in range(n)]
        self.held    = [Signal(n) for _ in range(n)] # Egress ports owned by each ingress.

        # # #

        busy     = Signal(n)
        grants   = Signal(n)
        ptr      = Signal(max=max(n, 2))
        ptr_next = Signal(max=max(n, 2))
        self.comb += busy.eq(reduce(or_, self.held))

        cases = {}
        for r in range(n):
            order = [(r + k)%n for k in range(n)]
            avail = ~busy
            g     = [None]*n
            for i in order:
                g[i]  = self.request[i] & ((self.mask[i] & ~avail) == 0)
                avail = avail & ~(Replicate(self.request[i], n) & self.mask[i])
            stmts = [grants.eq(Cat(*g))]
            # Advance past the first requester once it is granted.
            ptr_update = None
            for i in order:
                update = If(g[i], ptr_next.eq((i + 1)%n))
                if ptr_update is None:
                    ptr_update = If(self.request[i], update)
                else:
                    ptr_update = ptr_update.Elif(self.request[i], update)
            stmts.append(ptr_update)
            cases[r] = stmts
        self.comb += [
            ptr_next.eq(ptr),
            Case(ptr, cases),
        ]
        self.sync += ptr.eq(ptr_next)

        for i in range(n):
            self.comb += self.grant[i].eq(grants[i])
            self.sync += [
                If(self.release[i],
                    self.held[i].eq(0),
                ).Elif(grants[i],
                    self.held[i].eq(self.mask[i]),
                )
            ]

# Switch Egress ------------------------------------------------------------------------------------

class LiteEthSwitchEgress(LiteXModule):
    """Egress FIFO of a port. Its input ready doesn't depend on valid, as lockstep fan-out needs."""
    def __init__(self, dw, depth=8):
        self.fifo   = fifo = stream.SyncFIFO(eth_phy_description(dw), depth=depth, buffered=True)
        self.sink   = fifo.sink
        self.source = fifo.source

        # Statistics events (single-cycle pulses).
        self.ev_tx_frame = Signal()

        # # #

        self.comb += self.ev_tx_frame.eq(self.source.valid & self.source.ready & self.source.last)

# Switch -------------------------------------------------------------------------------------------

class LiteEthSwitch(LiteXModule):
    """Transparent learning Ethernet switch.

    An unmanaged layer-2 switch: frames are forwarded unmodified (no tagging, no FCS rewrite since
    errored frames are dropped) based on the destination MAC address, learned per port from source
    addresses. Broadcast, multicast and unknown unicast are flooded to all other ports, frames whose
    destination was learned on their ingress port are filtered.

    Ports are :class:`LiteEthSwitchPort` frame interfaces in the ``sys`` domain, attached to PHYs
    through MAC cores (:meth:`add_phy`) or to internal users (:meth:`connect`), e.g. the LiteEth
    UDP/IP stack (:class:`liteeth.switch.host.LiteEthSwitchUDPIPCore`) or a CPU Wishbone MAC.

    The switching bandwidth is ``dw*sys_clk_freq`` per port and direction, minus ~5 cycles per
    frame (lookup, allocation); floods are sent once to all their egress ports. E.g. with ``dw=32``,
    1Gbps line rate needs ``sys_clk_freq`` of ~32MHz or more, whatever the frame size.

    Parameters:
    - nports       : Number of ports.
    - dw           : Datapath width (bits).
    - clk_freq     : sys clock frequency, sets the default aging period.
    - table_depth  : MAC table entries (power of two).
    - aging_time   : MAC table aging time in seconds.
    - buffer_size  : Ingress buffer per port in bytes (power of two, at least one maximum frame).
    - param_depth  : Frames each ingress buffer can hold.
    - egress_depth : Egress FIFO depth in words.
    - with_csr     : Expose control/statistics CSRs.
    """
    def __init__(self, nports=3, dw=32, clk_freq=None,
        table_depth  = 256,
        aging_time   = 300,
        buffer_size  = 4096,
        param_depth  = 16,
        egress_depth = 8,
        with_csr     = True,
        ):
        assert nports >= 2
        assert dw in [8, 16, 32, 64]
        buffer_depth = buffer_size//(dw//8)
        assert buffer_depth == 2**log2_int(buffer_depth)
        assert buffer_size >= eth_mtu_default
        self.nports = nports
        self.dw     = dw
        self.ports  = ports = [LiteEthSwitchPort(dw) for _ in range(nports)]

        # # #

        # MAC Table.
        self.table = table = LiteEthSwitchMACTable(nports,
            depth      = table_depth,
            clk_freq   = clk_freq,
            aging_time = aging_time,
            with_csr   = with_csr,
        )

        # Allocator.
        self.allocator = allocator = LiteEthSwitchAllocator(nports)

        # Ingresses / Egresses.
        ingresses = []
        egresses  = []
        for n in range(nports):
            ingress = LiteEthSwitchIngress(dw, port=n, nports=nports,
                buffer_depth = buffer_depth,
                param_depth  = param_depth,
            )
            egress = LiteEthSwitchEgress(dw, depth=egress_depth)
            self.add_module(name=f"ingress{n}", module=ingress)
            self.add_module(name=f"egress{n}",  module=egress)
            self.comb += [
                # Port <-> Ingress/Egress.
                ports[n].sink.connect(ingress.sink),
                egress.source.connect(ports[n].source),
                # Ingress <-> MAC Table.
                ingress.lookup.connect(table.lookups[n]),
                table.results[n].connect(ingress.result),
                # Ingress <-> Allocator.
                allocator.request[n].eq(ingress.request),
                allocator.mask[n].eq(ingress.mask),
                ingress.grant.eq(allocator.grant[n]),
                allocator.release[n].eq(ingress.release),
            ]
            ingresses.append(ingress)
            egresses.append(egress)

        # Crossbar: lockstep fan-out of each ingress to the egresses it holds.
        held = allocator.held
        def others_ready(i, exclude=None):
            terms = [~held[i][p] | egresses[p].sink.ready
                for p in range(nports) if p not in (i, exclude)]
            return reduce(and_, terms, 1)
        for i, ingress in enumerate(ingresses):
            self.comb += ingress.source.ready.eq((held[i] != 0) & others_ready(i))
        for o, egress in enumerate(egresses):
            for i, ingress in enumerate(ingresses):
                if i == o:
                    continue
                self.comb += If(held[i][o],
                    ingress.source.connect(egress.sink, omit={"valid", "ready"}),
                    egress.sink.valid.eq(ingress.source.valid & others_ready(i, exclude=o)),
                )

        # CSRs.
        if with_csr:
            for n in range(nports):
                self.add_port_csrs(n, ingresses[n], egresses[n])

    def add_port_csrs(self, n, ingress, egress):
        nports = self.nports
        ingress._forward_mask = CSRStorage(nports, reset=2**nports - 1,
            description=f"Egress ports port {n} may forward to (port-based isolation).")
        ingress._rx_frames = CSRStatus(32, description="Received frames.")
        ingress._rx_drops  = CSRStatus(32, description="Received frames dropped (error, runt, overflow).")
        ingress._filtered  = CSRStatus(32, description="Frames filtered (destination on the ingress port or isolated).")
        ingress._flooded   = CSRStatus(32, description="Frames flooded (broadcast, multicast, unknown unicast).")
        egress._tx_frames  = CSRStatus(32, description="Transmitted frames.")
        self.comb += ingress.forward_mask.eq(ingress._forward_mask.storage)
        _counter(ingress, ingress._rx_frames, ingress.ev_rx_frame)
        _counter(ingress, ingress._rx_drops,  ingress.ev_rx_drop)
        _counter(ingress, ingress._filtered,  ingress.ev_filtered)
        _counter(ingress, ingress._flooded,   ingress.ev_flooded)
        _counter(egress,  egress._tx_frames,  egress.ev_tx_frame)

    def connect(self, n, user):
        """Connect port ``n`` to a frame user exposing ``sink`` (to transmit) and ``source``."""
        self.comb += [
            user.source.connect(self.ports[n].sink),
            self.ports[n].source.connect(user.sink),
        ]

    def add_phy(self, n, phy, cd=None, **kwargs):
        """Attach ``phy`` to port ``n`` through a :class:`LiteEthSwitchPHYPort` MAC core."""
        mac = LiteEthSwitchPHYPort(phy, self.dw, cd=cd, **kwargs)
        self.add_module(name=f"mac{n}", module=mac)
        self.connect(n, mac)
        return mac
