#
# This file is part of LiteEth.
#
# Copyright (c) 2026 Oliver Keller <oliver.keller@pm.me>
# SPDX-License-Identifier: BSD-2-Clause

from migen.genlib.roundrobin import RoundRobin, SP_CE

from litex.gen import *

from litex.soc.interconnect import stream
from litex.soc.interconnect.csr import *

# Layouts ------------------------------------------------------------------------------------------

# MAC addresses are carried in wire order: bits [7:0] hold the first octet on the wire, so bit 0 is
# the Individual/Group (multicast) bit.

def switch_lookup_layout():
    return [
        ("dst_mac", 48), # Destination MAC to look up.
        ("src_mac", 48), # Source MAC to learn.
        ("learn",    1), # Learn src_mac on the requesting port.
    ]

def switch_result_layout(nports):
    return [
        ("hit",  1),                    # dst_mac was found in the table.
        ("port", bits_for(nports - 1)), # Port dst_mac was learned on.
    ]

def mac_hash(mac, bits):
    """XOR-fold a 48-bit MAC address into a ``bits`` wide table index."""
    h = mac[0:bits]
    for i in range(bits, 48, bits):
        h = h ^ mac[i:min(i + bits, 48)]
    return h

def mac_wire_to_int(mac):
    """Wire-order Signal -> conventional big-endian (first octet as MSB) expression."""
    return Cat(*reversed([mac[8*i:8*(i + 1)] for i in range(6)]))

# MAC Table ----------------------------------------------------------------------------------------

class LiteEthSwitchMACTable(LiteXModule):
    """Learning MAC address table.

    Direct-mapped hash table in block RAM shared by all ports through a round-robin arbitrated
    engine. Each request performs a destination lookup and optionally (re)learns the source address
    on the requesting port, overwriting whatever occupied its slot.

    Entries are aged by a background sweeper visiting one entry every ``aging_period`` cycles: a
    visit clears the entry's ``age`` bit, or invalidates it when the bit was already clear. Since
    learning sets the bit, an idle address expires after one to two full sweeps. ``aging_time``
    (seconds) sets the default period so that an idle address expires within ``aging_time``.
    """
    def __init__(self, nports, depth=256, clk_freq=None, aging_time=300, with_csr=True):
        assert depth == 2**log2_int(depth)
        abits = log2_int(depth)
        pbits = bits_for(nports - 1)

        self.lookups = [stream.Endpoint(switch_lookup_layout())       for _ in range(nports)]
        self.results = [stream.Endpoint(switch_result_layout(nports)) for _ in range(nports)]

        # Default aging period.
        self.aging_reset = 0
        if clk_freq is not None and aging_time:
            self.aging_reset = int(aging_time*clk_freq/(2*depth))

        # Control.
        self.flush        = Signal()                           # Pulse: invalidate every entry.
        self.aging_period = Signal(32, reset=self.aging_reset) # Cycles between sweeper visits (0: off).

        # Debug read access.
        self.read_index = Signal(abits)
        self.read       = Signal()     # Pulse: fetch entry read_index into the read_* signals.
        self.read_valid = Signal()
        self.read_age   = Signal()
        self.read_port  = Signal(pbits)
        self.read_mac   = Signal(48)   # Wire order.

        # Statistics events (single-cycle pulses).
        self.ev_hit  = Signal()
        self.ev_miss = Signal()

        # # #

        # Entry: MAC (wire order) | port | age | valid.
        entry_layout = [("mac", 48), ("port", pbits), ("age", 1), ("valid", 1)]
        def entry(s):
            r = Record(entry_layout)
            self.comb += r.raw_bits().eq(s)
            return r

        mem = Memory(layout_len(entry_layout), depth, init=[0]*depth)
        rd  = mem.get_port()
        wr  = mem.get_port(write_capable=True)
        self.specials += mem, rd, wr
        rd_entry = entry(rd.dat_r)
        wr_entry = Record(entry_layout)
        self.comb += wr.dat_w.eq(wr_entry.raw_bits())

        # Request arbitration.
        self.rr = rr = RoundRobin(nports, SP_CE)
        self.comb += rr.request.eq(Cat(*[l.valid for l in self.lookups]))
        lookup_valid   = Array(l.valid   for l in self.lookups)[rr.grant]
        lookup_dst_mac = Array(l.dst_mac for l in self.lookups)[rr.grant]
        lookup_src_mac = Array(l.src_mac for l in self.lookups)[rr.grant]
        lookup_learn   = Array(l.learn   for l in self.lookups)[rr.grant]

        # Latched request.
        req_port = Signal(pbits)
        req_dst  = Signal(48)
        req_src  = Signal(48)
        req_lrn  = Signal()
        res_hit  = Signal()
        res_port = Signal(pbits)

        # Results.
        for r in self.results:
            self.comb += [
                r.hit.eq(res_hit),
                r.port.eq(res_port),
                r.last.eq(1),
            ]
        result_ready = Array(r.ready for r in self.results)[req_port]

        # Aging timer.
        age_timer   = Signal(32)
        age_pending = Signal()
        age_ack     = Signal()
        age_adr     = Signal(abits)
        self.sync += [
            If(age_ack, age_pending.eq(0)),
            # Count up to the period, so that a new period applies immediately.
            If(self.aging_period == 0,
                age_timer.eq(0),
            ).Elif(age_timer >= (self.aging_period - 1),
                age_timer.eq(0),
                age_pending.eq(1),
            ).Else(
                age_timer.eq(age_timer + 1),
            ),
        ]

        # Flush / debug read requests (a new request wins over the completion of the previous one).
        flush_pending = Signal()
        flush_ack     = Signal()
        flush_adr     = Signal(abits)
        read_pending  = Signal()
        read_ack      = Signal()
        self.sync += [
            If(flush_ack,  flush_pending.eq(0)),
            If(self.flush, flush_pending.eq(1)),
            If(read_ack,   read_pending.eq(0)),
            If(self.read,  read_pending.eq(1)),
        ]

        # Engine.
        self.fsm = fsm = FSM(reset_state="IDLE")
        fsm.act("IDLE",
            rr.ce.eq(1),
            rd.adr.eq(mac_hash(lookup_dst_mac, abits)),
            If(flush_pending,
                NextValue(flush_adr, 0),
                NextState("FLUSH"),
            ).Elif(lookup_valid,
                Array(l.ready for l in self.lookups)[rr.grant].eq(1),
                NextValue(req_port, rr.grant),
                NextValue(req_dst,  lookup_dst_mac),
                NextValue(req_src,  lookup_src_mac),
                NextValue(req_lrn,  lookup_learn),
                NextState("LOOKUP"),
            ).Elif(age_pending,
                NextState("AGE-READ"),
            ).Elif(read_pending,
                NextState("DEBUG-READ"),
            )
        )
        fsm.act("LOOKUP",
            # rd.dat_r holds the entry at hash(dst), addressed from IDLE.
            NextValue(res_hit,  rd_entry.valid & (rd_entry.mac == req_dst)),
            NextValue(res_port, rd_entry.port),
            NextState("RESPOND"),
        )
        fsm.act("RESPOND",
            Array(r.valid for r in self.results)[req_port].eq(1),
            If(result_ready,
                self.ev_hit.eq(res_hit),
                self.ev_miss.eq(~res_hit),
                # Learn: (re)bind the source address to the requesting port.
                wr.adr.eq(mac_hash(req_src, abits)),
                wr_entry.mac.eq(req_src),
                wr_entry.port.eq(req_port),
                wr_entry.age.eq(1),
                wr_entry.valid.eq(1),
                wr.we.eq(req_lrn),
                NextState("IDLE"),
            )
        )
        fsm.act("AGE-READ",
            rd.adr.eq(age_adr),
            NextState("AGE-WRITE"),
        )
        fsm.act("AGE-WRITE",
            age_ack.eq(1),
            # Clear the age bit; invalidate when it was already clear. (Fields are assigned one by
            # one: a Cat assignment would make them multi-driven in the generated Verilog.)
            wr.adr.eq(age_adr),
            wr_entry.mac.eq(rd_entry.mac),
            wr_entry.port.eq(rd_entry.port),
            wr_entry.age.eq(0),
            wr_entry.valid.eq(rd_entry.age),
            wr.we.eq(rd_entry.valid),
            NextValue(age_adr, age_adr + 1),
            NextState("IDLE"),
        )
        fsm.act("FLUSH",
            wr.adr.eq(flush_adr),
            wr.we.eq(1), # wr_entry defaults to zero: invalid.
            NextValue(flush_adr, flush_adr + 1),
            If(flush_adr == (depth - 1),
                flush_ack.eq(1),
                NextState("IDLE"),
            )
        )
        fsm.act("DEBUG-READ",
            rd.adr.eq(self.read_index),
            NextState("DEBUG-LATCH"),
        )
        fsm.act("DEBUG-LATCH",
            NextValue(self.read_valid, rd_entry.valid),
            NextValue(self.read_age,   rd_entry.age),
            NextValue(self.read_port,  rd_entry.port),
            NextValue(self.read_mac,   rd_entry.mac),
            read_ack.eq(1),
            NextState("IDLE"),
        )

        if with_csr:
            self.add_csr(abits, pbits)

    def add_csr(self, abits, pbits):
        self._control = CSRStorage(fields=[
            CSRField("flush", size=1, offset=0, pulse=True, description="Invalidate all entries."),
        ])
        self._aging_period = CSRStorage(32, reset=self.aging_reset,
            description="Cycles between aging sweeper visits of two consecutive entries (0: aging disabled).")
        self._entry_index = CSRStorage(abits, description="Entry to read; a write latches it into ``entry_*``.")
        self._entry_info  = CSRStatus(fields=[
            CSRField("valid", size=1,     offset=0),
            CSRField("age",   size=1,     offset=1),
            CSRField("port",  size=pbits, offset=8),
        ])
        self._entry_mac = CSRStatus(48, description="MAC address of the read entry.")
        self._hits      = CSRStatus(32, description="Destination lookups that hit.")
        self._misses    = CSRStatus(32, description="Destination lookups that missed (flooded).")

        self.comb += [
            self.flush.eq(self._control.fields.flush),
            self.aging_period.eq(self._aging_period.storage),
            self.read_index.eq(self._entry_index.storage),
            self.read.eq(self._entry_index.re),
            self._entry_info.fields.valid.eq(self.read_valid),
            self._entry_info.fields.age.eq(self.read_age),
            self._entry_info.fields.port.eq(self.read_port),
            self._entry_mac.status.eq(mac_wire_to_int(self.read_mac)),
        ]
        self.sync += [
            If(self.ev_hit,  self._hits.status.eq(  self._hits.status   + 1)),
            If(self.ev_miss, self._misses.status.eq(self._misses.status + 1)),
        ]
