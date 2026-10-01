#
# This file is part of LiteEth.
#
# Copyright (c) 2026 Oliver Keller <oliver.keller@pm.me>
# SPDX-License-Identifier: BSD-2-Clause

import random
import unittest

from migen import *
from migen.sim import passive

from liteeth.switch import LiteEthSwitch, LiteEthSwitchAllocator

# Helpers ------------------------------------------------------------------------------------------

BCAST = 0xffffffffffff
MCAST = 0x01005e000001

def mac(n):
    return 0x02_00_00_00_00_00 | n

def frame(dst, src, length=60, seed=0):
    """Ethernet frame (without FCS) as a list of bytes."""
    hdr     = dst.to_bytes(6, "big") + src.to_bytes(6, "big") + bytes([0x88, 0xb5])
    payload = bytes((seed*31 + i) & 0xff for i in range(length - len(hdr)))
    return list(hdr + payload)


def inject(ep, data, nb=4, idle=0):
    """Inject a frame on a 32-bit port sink, then idle."""
    for i in range(0, len(data), nb):
        word = data[i:i + nb]
        yield ep.valid.eq(1)
        yield ep.last.eq(i + nb >= len(data))
        yield ep.data.eq(sum(b << 8*k for k, b in enumerate(word)))
        yield ep.be.eq((1 << len(word)) - 1)
        yield
        while not (yield ep.ready):
            yield
    yield ep.valid.eq(0)
    for _ in range(idle):
        yield


class SwitchBench:
    """Drives frames into switch ports and collects what each port transmits."""
    def __init__(self, dut, seed=0, rx_gap=0, tx_stall=0):
        self.dut      = dut
        self.nb       = dut.dw//8
        self.prng     = random.Random(seed)
        self.rx_gap   = rx_gap   # Max idle cycles between injected frames.
        self.tx_stall = tx_stall # 1/tx_stall probability of a not-ready cycle on egress.
        self.queues   = [[] for _ in dut.ports] # (bytes, error) to inject per port.
        self.received = [[] for _ in dut.ports]
        self.ingress_done = [False for _ in dut.ports]

    def send(self, port, data, error=False):
        self.queues[port].append((data, error))

    def driver(self, n):
        ep, nb = self.dut.ports[n].sink, self.nb
        for data, error in self.queues[n]:
            words = [data[i:i + nb] for i in range(0, len(data), nb)]
            for i, word in enumerate(words):
                last = (i == len(words) - 1)
                be   = (1 << len(word)) - 1
                yield ep.valid.eq(1)
                yield ep.first.eq(i == 0)
                yield ep.last.eq(last)
                yield ep.data.eq(sum(b << 8*k for k, b in enumerate(word)))
                yield ep.be.eq(be)
                yield ep.error.eq(be if (error and last) else 0)
                yield
                while not (yield ep.ready):
                    yield
            yield ep.valid.eq(0)
            for _ in range(self.prng.randint(0, self.rx_gap)):
                yield
        self.ingress_done[n] = True

    @passive
    def collector(self, n):
        ep, nb = self.dut.ports[n].source, self.nb
        current = []
        while True:
            ready = int(self.tx_stall == 0 or self.prng.randrange(self.tx_stall) != 0)
            yield ep.ready.eq(ready)
            yield
            if (yield ep.valid) and (yield ep.ready):
                data, be = (yield ep.data), (yield ep.be)
                for k in range(nb):
                    if (be >> k) & 1:
                        current.append((data >> 8*k) & 0xff)
                if (yield ep.last):
                    self.received[n].append(current)
                    current = []

    def run(self, cycles=20000, extra=None):
        def main():
            for _ in range(cycles):
                if all(self.ingress_done):
                    break
                yield
            # Drain.
            for _ in range(2000):
                yield
        generators = [main()]
        generators += [self.driver(n)    for n in range(len(self.dut.ports))]
        generators += [self.collector(n) for n in range(len(self.dut.ports))]
        if extra is not None:
            generators += extra
        run_simulation(self.dut, generators)


def new_switch(nports=3, dw=32, **kwargs):
    kwargs.setdefault("table_depth", 16)
    kwargs.setdefault("aging_time",  0)
    kwargs.setdefault("buffer_size", 2048)
    return LiteEthSwitch(nports=nports, dw=dw, with_csr=False, **kwargs)

# Switch Tests -------------------------------------------------------------------------------------

class TestSwitch(unittest.TestCase):
    def check_learning(self, dw):
        dut   = new_switch(dw=dw)
        bench = SwitchBench(dut, seed=dw)
        a, b  = mac(0xa), mac(0xb)
        f0 = frame(b, a, 64,  seed=0) # A -> B unknown: flooded.
        f1 = frame(a, b, 61,  seed=1) # B -> A: A learned on port 0.
        f2 = frame(b, a, 123, seed=2) # A -> B: B learned on port 1.
        bench.send(0, f0)
        # Delay the reply so that the first frame is learned first.
        bench.send(1, frame(mac(0xff), mac(0xfe), 60, seed=9), error=True)          # Dropped.
        bench.send(1, f1)
        bench.send(0, frame(mac(0xff), mac(0xfe), 60, seed=8), error=True)          # Dropped.
        bench.send(0, frame(mac(0xff), mac(0xfe), 60, seed=7), error=True)          # Dropped.
        bench.send(0, f2)
        bench.run()
        self.assertEqual(bench.received[0], [f1])
        self.assertEqual(bench.received[1], [f0, f2])
        self.assertEqual(bench.received[2], [f0])

    def test_learning_dw8(self):
        self.check_learning(8)

    def test_learning_dw32(self):
        self.check_learning(32)

    def test_learning_dw64(self):
        self.check_learning(64)

    def test_broadcast_multicast_flooded(self):
        dut   = new_switch()
        bench = SwitchBench(dut)
        fb = frame(BCAST, mac(1), 60, seed=1)
        fm = frame(MCAST, mac(1), 70, seed=2)
        bench.send(2, fb)
        bench.send(2, fm)
        bench.run()
        self.assertEqual(bench.received[0], [fb, fm])
        self.assertEqual(bench.received[1], [fb, fm])
        self.assertEqual(bench.received[2], [])

    def test_same_port_filtered(self):
        dut   = new_switch()
        bench = SwitchBench(dut)
        a, c  = mac(0xa), mac(0xc)
        f0 = frame(BCAST, c, 60, seed=0) # C (behind port 0) announces itself.
        f1 = frame(c,     a, 60, seed=1) # A (port 0) -> C (port 0): filtered.
        f2 = frame(BCAST, a, 60, seed=2) # Sentinel.
        for f in [f0, f1, f2]:
            bench.send(0, f)
        bench.run()
        self.assertEqual(bench.received[1], [f0, f2])
        self.assertEqual(bench.received[2], [f0, f2])
        self.assertEqual(bench.received[0], [])

    def test_errored_and_runt_frames_dropped(self):
        dut   = new_switch()
        bench = SwitchBench(dut)
        a, b  = mac(0xa), mac(0xb)
        bench.send(0, frame(BCAST, a, 60, seed=0), error=True) # Errored: dropped, A not learned.
        bench.send(0, frame(BCAST, a, 60, seed=1)[:13])        # Runt: dropped.
        good = frame(BCAST, b, 60, seed=2)
        bench.send(1, good)
        bench.run()
        self.assertEqual(bench.received[0], [good])
        self.assertEqual(bench.received[1], [])
        self.assertEqual(bench.received[2], [good])

    def test_station_move(self):
        dut   = new_switch()
        bench = SwitchBench(dut, rx_gap=0)
        a, b  = mac(0xa), mac(0xb)
        seq = [
            (0, frame(BCAST, a, 60, seed=0)), # A on port 0.
            (1, frame(BCAST, b, 60, seed=1)), # B on port 1.
            (1, frame(a,     b, 60, seed=2)), # -> port 0.
            (2, frame(BCAST, a, 60, seed=3)), # A moves to port 2.
            (1, frame(a,     b, 60, seed=4)), # -> port 2.
        ]
        # Serialize the sequence: each frame is injected once the previous one was forwarded.
        def sequencer():
            for port, f in seq:
                yield from inject(dut.ports[port].sink, f, idle=200)
        bench.run(extra=[sequencer()])
        self.assertEqual(bench.received[0], [seq[1][1], seq[2][1], seq[3][1]])
        self.assertEqual(bench.received[1], [seq[0][1], seq[3][1]])
        self.assertEqual(bench.received[2], [seq[0][1], seq[1][1], seq[4][1]])

    def test_concurrent_floods_no_deadlock(self):
        self.check_concurrent_floods()

    def test_concurrent_floods_no_deadlock_store_and_forward(self):
        self.check_concurrent_floods(egress_store_and_forward=True)

    def check_concurrent_floods(self, **kwargs):
        # Every port floods simultaneously while egresses stall randomly: each port must receive
        # every frame from the other ports, in order per source.
        dut   = new_switch(**kwargs)
        bench = SwitchBench(dut, seed=3, rx_gap=4, tx_stall=3)
        sent  = [[], [], []]
        prng  = random.Random(5)
        for n in range(3):
            for i in range(12):
                f = frame(BCAST, mac(n), prng.randint(60, 200), seed=16*n + i)
                bench.send(n, f)
                sent[n].append(f)
        bench.run(cycles=40000)
        for o in range(3):
            got = bench.received[o]
            for i in range(3):
                from_i = [f for f in got if f[6:12] == list(mac(i).to_bytes(6, "big"))]
                if i == o:
                    self.assertEqual(from_i, [])
                else:
                    self.assertEqual(from_i, sent[i])

    def test_mixed_traffic(self):
        # Random unicast traffic between stations behind each port; once every station announced
        # itself, unicast frames must only reach their destination port, in order per source.
        dut      = new_switch()
        bench    = SwitchBench(dut, seed=11, rx_gap=6, tx_stall=4)
        prng     = random.Random(12)
        stations = {mac(0x10 + n): n for n in range(3)}
        for m, p in stations.items():
            bench.send(p, frame(BCAST, m, 60, seed=p))
        traffic = []
        for i in range(30):
            src, dst = prng.sample(list(stations), 2)
            f = frame(dst, src, prng.randint(60, 300), seed=100 + i)
            traffic.append((stations[src], f, stations[dst]))
        def late_driver():
            # Start once announcements have been learned.
            for _ in range(1000):
                yield
            for port, f, _ in traffic:
                yield from inject(dut.ports[port].sink, f)
                yield
            for _ in range(2000):
                yield
        bench.run(cycles=1000, extra=[late_driver()])
        for o in range(3):
            got = bench.received[o]
            self.assertEqual(sorted(got[:2]),
                sorted(frame(BCAST, m, 60, seed=p) for m, p in stations.items() if p != o))
            for s in range(3):
                self.assertEqual([f for f in got[2:] if f[6:12] == list(mac(0x10 + s).to_bytes(6, "big"))],
                                 [f for p, f, d in traffic if d == o and p == s])

    def test_aging(self):
        # Default period of ~5.9s at 100MHz: lowering it at runtime must apply immediately.
        dut   = new_switch(clk_freq=100e6, aging_time=300)
        bench = SwitchBench(dut)
        a, b  = mac(0xa), mac(0xb)
        fa = frame(BCAST, a, 60, seed=0)
        fb = frame(a,     b, 60, seed=1)
        def sequencer():
            def send(port, f):
                yield from inject(dut.ports[port].sink, f, idle=100)
            yield from send(0, fa) # Learn A on port 0.
            yield from send(1, fb) # Known: port 0 only.
            self.assertGreater((yield dut.table.aging_period), 10**8)
            yield dut.table.aging_period.eq(4)
            for _ in range(16*4*3): # More than two sweeps over the 16 entries.
                yield
            yield dut.table.aging_period.eq(0)
            yield from send(1, fb) # A expired: flooded.
        bench.run(extra=[sequencer()])
        self.assertEqual(bench.received[0], [fb, fb])
        self.assertEqual(bench.received[1], [fa])
        self.assertEqual(bench.received[2], [fa, fb])

    def test_flush(self):
        dut   = new_switch()
        bench = SwitchBench(dut)
        a, b  = mac(0xa), mac(0xb)
        fa = frame(BCAST, a, 60, seed=0)
        fb = frame(a,     b, 60, seed=1)
        def sequencer():
            def send(port, f):
                yield from inject(dut.ports[port].sink, f, idle=100)
            yield from send(0, fa)
            yield from send(1, fb)
            yield dut.table.flush.eq(1)
            yield
            yield dut.table.flush.eq(0)
            for _ in range(40):
                yield
            yield from send(1, fb)
        bench.run(extra=[sequencer()])
        self.assertEqual(bench.received[0], [fb, fb])
        self.assertEqual(bench.received[2], [fa, fb])

    def test_unicast_flows_in_parallel(self):
        # 0 -> 1 and 1 -> 0 don't share egress ports: both must be forwarded concurrently.
        dut   = new_switch()
        a, b  = mac(0xa), mac(0xb)
        busy  = {"both": 0}
        def monitor():
            while True:
                both = (yield dut.ports[0].source.valid) and (yield dut.ports[1].source.valid)
                busy["both"] += int(bool(both))
                yield
        bench = SwitchBench(dut)
        bench.send(0, frame(BCAST, a, 60, seed=0))
        bench.send(1, frame(BCAST, b, 60, seed=1))
        for i in range(4):
            bench.send(0, frame(b, a, 256, seed=10 + i))
            bench.send(1, frame(a, b, 256, seed=20 + i))
        bench.run(extra=[passive(monitor)()])
        self.assertEqual(len(bench.received[0]), 5)
        self.assertEqual(len(bench.received[1]), 5)
        self.assertGreater(busy["both"], 100)

    def check_line_rate(self, dst, egress):
        # Back-to-back 60-byte frames (64 with FCS: 84 byte times on the wire with preamble and
        # IFG) must be forwarded faster than 1Gbps line rate at 50MHz: 33.6 cycles per frame.
        dut    = new_switch()
        bench  = SwitchBench(dut)
        a, b   = mac(0xa), mac(0xb)
        nframes = 24
        ends   = {o: [] for o in egress}
        cycle  = [0]
        def monitor():
            while True:
                yield
                cycle[0] += 1
                for o in egress:
                    ep = dut.ports[o].source
                    if (yield ep.valid) and (yield ep.ready) and (yield ep.last):
                        ends[o].append(cycle[0])
        bench.send(1, frame(BCAST, b, 60, seed=0)) # Learn B on port 1.
        def burst():
            for _ in range(200):
                yield
            for i in range(nframes):
                yield from inject(dut.ports[0].sink, frame(dst, a, 60, seed=i))
            for _ in range(500):
                yield
        bench.run(extra=[burst(), passive(monitor)()])
        for o in egress:
            got = ends[o][-nframes:]
            self.assertEqual(len(got), nframes)
            # Steady state: skip the first frame, buffered while the next ones arrive.
            spacing = (got[-1] - got[1])/(nframes - 2)
            self.assertLessEqual(spacing, 33.6, f"port {o}: {spacing:.1f} cycles/frame")

    def test_line_rate_unicast(self):
        self.check_line_rate(mac(0xb), egress=[1])

    def test_line_rate_flood(self):
        self.check_line_rate(BCAST, egress=[1, 2])

# Line-rate Load Tests -----------------------------------------------------------------------------

class TestSwitchLineRate(unittest.TestCase):
    """Sustained 1Gbps on every ingress and egress at once, as on the Colorlight i9 (dw=32, 50MHz).

    Each ingress receives frames at exactly line rate (preamble, FCS and IFG included) and each
    egress is drained at line rate: no frame may be dropped, lost, reordered or corrupted.
    """
    sys_clk_freq   = 50e6
    word_cycles    = 4*8e-9*sys_clk_freq # Cycles per 32-bit word at 1Gbps: 1.6.
    switch_kwargs  = {}

    def run_load(self, pattern, length, nframes):
        dut      = new_switch(clk_freq=self.sys_clk_freq, buffer_size=4096, **self.switch_kwargs)
        overhead = 8 + 4 + 12 # Preamble, FCS and IFG bytes.
        wire     = (length + overhead)*8e-9*self.sys_clk_freq # Cycles per frame on the wire.
        start    = 400 # Once every station has been learned.
        cycle    = [0]
        drops    = [0]
        sent     = {p: [] for p in pattern}
        got      = [[] for _ in range(3)]
        learn    = {p: frame(BCAST, mac(p), 60, seed=255) for p in range(3)} # Unlike sent payloads.

        def clock():
            while True:
                yield
                cycle[0] += 1

        def ingress(p, dst):
            src = mac(p)
            yield from inject(dut.ports[p].sink, learn[p])
            for i in range(nframes):
                f = frame(dst, src, length, seed=i)
                sent[p].append(f)
                for k in range(0, len(f), 4):
                    while cycle[0] < start + int(i*wire + (k//4)*self.word_cycles):
                        yield
                    word = f[k:k + 4]
                    yield dut.ports[p].sink.valid.eq(1)
                    yield dut.ports[p].sink.last.eq(k + 4 >= len(f))
                    yield dut.ports[p].sink.data.eq(sum(b << 8*j for j, b in enumerate(word)))
                    yield dut.ports[p].sink.be.eq((1 << len(word)) - 1)
                    yield
                    yield dut.ports[p].sink.valid.eq(0)

        def egress(p):
            # Wire model: a word every word_cycles, plus preamble/FCS/IFG time after each frame.
            ep, current, c, free = dut.ports[p].source, [], 0, 0.0
            while True:
                yield ep.ready.eq(int(c >= free))
                yield
                if (yield ep.valid) and (yield ep.ready):
                    # Keep the fractional schedule while busy; restart it after an idle wire.
                    free = (free if c < free + 1 else c) + self.word_cycles
                    data, be = (yield ep.data), (yield ep.be)
                    current += [(data >> 8*j) & 0xff for j in range(4) if (be >> j) & 1]
                    if (yield ep.last):
                        free += overhead/4*self.word_cycles
                        if current not in learn.values():
                            got[p].append(current)
                        current = []
                c += 1

        def drop_monitor():
            while True:
                yield
                for n in range(3):
                    drops[0] += (yield getattr(dut, f"ingress{n}").ev_rx_drop)

        def main():
            for _ in range(start + int(nframes*wire) + 2000):
                yield

        run_simulation(dut, [main(), passive(clock)(), passive(drop_monitor)()] +
            [ingress(p, dst) for p, dst in pattern.items()] +
            [passive(egress)(p) for p in range(3)])
        return sent, got, drops[0]

    def check(self, pattern, length, nframes):
        sent, got, drops = self.run_load({p: (BCAST if d is None else mac(d)) for p, d in pattern.items()},
            length, nframes)
        self.assertEqual(drops, 0)
        for o in range(3):
            # Frames from each source, in order; a flood (None) reaches every other port.
            expected = [f for p in sorted(pattern) for f in sent[p] if pattern[p] in (o, None) and p != o]
            for p in pattern:
                from_p = [f for f in got[o] if f[6:12] == list(mac(p).to_bytes(6, "big"))]
                self.assertEqual(from_p, [f for f in expected if f[6:12] == list(mac(p).to_bytes(6, "big"))],
                    f"egress {o}, from port {p}")
            self.assertEqual(len(got[o]), len(expected))

    def test_three_ports_permutation_min_frames(self):
        self.check({0: 1, 1: 2, 2: 0}, length=60, nframes=100)

    def test_bridge_full_duplex_min_frames(self):
        self.check({0: 1, 1: 0}, length=60, nframes=100)

    def test_bridge_full_duplex_max_frames(self):
        self.check({0: 1, 1: 0}, length=1514, nframes=8)

    def test_flood_min_frames(self):
        self.check({0: None}, length=60, nframes=100)

class TestSwitchLineRateStoreAndForward(TestSwitchLineRate):
    """Same load with store-and-forward egresses: whole frames are held, throughput is unchanged."""
    switch_kwargs = {"egress_store_and_forward": True}

# Mixed Speed Tests --------------------------------------------------------------------------------

class TestSwitchMixedSpeed(unittest.TestCase):
    """Floods towards a 1Gbps and a 100Mbps egress port (dw=32, 50MHz).

    A flood reaches all its egress ports in lockstep, at the pace of the slowest. A PHY port must
    never run out of data within a frame (its MAC/PHY transmit path can't pause): with cut-through
    egresses the 1Gbps port starves, with store-and-forward egresses it only ever transmits complete
    frames.
    """
    word_cycles = {1: 1.6, 2: 16.0} # 1Gbps, 100Mbps.

    def run_floods(self, nframes, length=1518, gap=0, **kwargs):
        dut      = new_switch(buffer_size=4096, **kwargs)
        starved  = {1: 0, 2: 0}
        got      = {1: [], 2: []}
        sent     = [frame(BCAST, mac(0), length, seed=i) for i in range(nframes)]

        def egress(p):
            # A wire wanting a word every word_cycles[p]; counts starvation within frames.
            ep, c, free, in_frame, current = dut.ports[p].source, 0, 0.0, False, []
            while True:
                want = c >= free
                yield ep.ready.eq(int(want))
                yield
                if want and (yield ep.valid):
                    free = (free if c < free + 1 else c) + self.word_cycles[p]
                    data, be = (yield ep.data), (yield ep.be)
                    current += [(data >> 8*j) & 0xff for j in range(4) if (be >> j) & 1]
                    in_frame = not (yield ep.last)
                    if not in_frame:
                        got[p].append(current)
                        current = []
                elif want and in_frame:
                    starved[p] += 1
                c += 1

        def main():
            for f in sent:
                yield from inject(dut.ports[0].sink, f, idle=gap)
            for _ in range(int(nframes*(length//4 + 8)*self.word_cycles[2]) + 1000):
                yield

        run_simulation(dut, [main()] + [passive(egress)(p) for p in [1, 2]])
        for p in [1, 2]:
            self.assertEqual(got[p], sent)
        return starved[1]

    def test_cut_through_starves_fast_port(self):
        # Documents why store-and-forward egresses are needed with mixed speeds.
        self.assertGreater(self.run_floods(nframes=1), 1000)

    def test_store_and_forward_never_starves(self):
        for ports in [True, [1, 2]]:
            with self.subTest(egress_store_and_forward=ports):
                self.assertEqual(self.run_floods(nframes=4, egress_store_and_forward=ports), 0)

    def test_store_and_forward_max_frame(self):
        # The egress buffer holds the largest frame the ingress accepts. The ingress buffer holds a
        # single one: leave time for the first frame to move on before sending the second.
        self.assertEqual(self.run_floods(nframes=2, length=4096, gap=1100,
            egress_store_and_forward=True), 0)

# Allocator Tests ----------------------------------------------------------------------------------

class TestSwitchAllocator(unittest.TestCase):
    def test_flood_not_starved(self):
        # Ports 1 and 2 keep egresses 2 and 1 busy with out-of-phase unicast bursts, so they are
        # never both idle on their own; port 0's flood to {1, 2} must still be granted.
        dut    = LiteEthSwitchAllocator(3)
        grants = {0: [], 1: [], 2: []}
        cycle  = [0]

        def clock():
            while True:
                yield
                cycle[0] += 1

        def agent(i, mask, delay, hold, once):
            for _ in range(delay):
                yield
            while True:
                yield dut.request[i].eq(1)
                yield dut.mask[i].eq(mask)
                yield
                while not (yield dut.grant[i]):
                    yield
                grants[i].append(cycle[0])
                yield dut.request[i].eq(0)
                for _ in range(hold):
                    yield
                yield dut.release[i].eq(1)
                yield
                yield dut.release[i].eq(0)
                if once:
                    return

        def main():
            yield from agent(0, 0b110, delay=4, hold=2, once=True)

        run_simulation(dut, [
            main(),
            passive(clock)(),
            passive(agent)(1, 0b100, 0, 5, False),
            passive(agent)(2, 0b010, 2, 7, False),
        ])
        self.assertEqual(len(grants[0]), 1)
        self.assertLess(grants[0][0], 30)
        # While port 0 waited, ports 1/2 were not re-granted past its reservation.
        self.assertTrue(all(c <= grants[0][0] for c in grants[1][:1] + grants[2][:1]))

if __name__ == "__main__":
    unittest.main()
