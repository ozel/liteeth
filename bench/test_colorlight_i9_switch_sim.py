#!/usr/bin/env python3

#
# This file is part of LiteEth.
#
# Copyright (c) 2026 Oliver Keller <oliver.keller@pm.me>
# SPDX-License-Identifier: BSD-2-Clause

"""System test of the Colorlight i9 3-port switch, in simulation with two TAP interfaces.

The Verilator simulation built by ``colorlight_i9_switch.py --sim`` bridges each RGMII PHY to a TAP
interface. Each TAP is moved into its own network namespace (so the kernel can't short-circuit the
traffic), then the test exchanges traffic between the namespaces through the switch, with the
internal host (ICMP + Etherbone) and checks the switch state over Etherbone.

    sw0: tap0 192.168.1.100 -- PHY0 --+
                                      +-- switch -- host 192.168.1.50 (ICMP, Etherbone)
    sw1: tap1 192.168.1.101 -- PHY1 --+

Run as root (TAP interfaces, namespaces):
    ./test_colorlight_i9_switch_sim.py               # Build the simulation, then test.
    ./test_colorlight_i9_switch_sim.py --no-build    # Reuse a previous build.
"""

import os
import re
import sys
import time
import ctypes
import socket
import argparse
import contextlib
import subprocess

from litex.tools.remote.comm_udp import CommUDP

# Setup --------------------------------------------------------------------------------------------

HOST_IP  = "192.168.1.50"
HOST_MAC = 0x10e2d5000000
NS = [
    # Namespace, TAP, IP, MAC.
    ("sw0", "tap0", "192.168.1.100", 0x020000000010),
    ("sw1", "tap1", "192.168.1.101", 0x020000000011),
]

def mac_str(mac):
    return ":".join(f"{(mac >> (8*i)) & 0xff:02x}" for i in reversed(range(6)))

def sh(cmd, check=True, capture=False, timeout=60):
    r = subprocess.run(cmd, shell=True, text=True, timeout=timeout,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL, stderr=subprocess.STDOUT)
    if check and r.returncode != 0:
        raise RuntimeError(f"'{cmd}' failed:\n{r.stdout or ''}")
    return r

# Network namespaces -------------------------------------------------------------------------------

_libc         = ctypes.CDLL(None, use_errno=True)
_CLONE_NEWNET = 0x40000000

@contextlib.contextmanager
def netns(name):
    """Run the block in network namespace ``name``; sockets created there stay in it."""
    own    = os.open("/proc/self/ns/net", os.O_RDONLY)
    target = os.open(f"/run/netns/{name}", os.O_RDONLY)
    try:
        if _libc.setns(target, _CLONE_NEWNET) != 0:
            raise OSError(ctypes.get_errno(), f"setns({name})")
        yield
    finally:
        _libc.setns(own, _CLONE_NEWNET)
        os.close(own)
        os.close(target)

def ping(ns, ip, count=3, size=56, timeout=5):
    r = sh(f"ip netns exec {ns} ping -n -c {count} -i 0.2 -W {timeout} -s {size} -M do {ip}",
        check=False, capture=True, timeout=count*(timeout + 1) + 10)
    m = re.search(r"(\d+) received", r.stdout)
    return int(m.group(1)) if m else 0

# Simulation ---------------------------------------------------------------------------------------

class SwitchSim:
    def __init__(self, gateware_dir, log):
        self.gateware_dir = gateware_dir
        self.log          = log
        self.proc         = None

    def start(self):
        self.stop()
        env = dict(os.environ)
        self.proc = subprocess.Popen(["./obj_dir/Vsim"], cwd=self.gateware_dir, env=env,
            stdout=open(self.log, "w"), stderr=subprocess.STDOUT, start_new_session=True)
        for _ in range(200):
            if all(os.path.exists(f"/sys/class/net/{tap}") for _, tap, _, _ in NS):
                break
            if self.proc.poll() is not None:
                raise RuntimeError(f"Simulation exited, see {self.log}")
            time.sleep(0.05)
        else:
            raise RuntimeError("TAP interfaces did not appear")
        for ns, tap, ip, mac in NS:
            sh(f"ip netns add {ns}")
            sh(f"ip link set {tap} netns {ns}")
            sh(f"ip netns exec {ns} ip link set lo up")
            sh(f"ip netns exec {ns} ip link set {tap} address {mac_str(mac)}")
            sh(f"ip netns exec {ns} ip addr add {ip}/24 dev {tap}")
            sh(f"ip netns exec {ns} ip link set {tap} up")

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        for ns, _, _, _ in NS:
            sh(f"ip netns del {ns}", check=False)

# Switch access over Etherbone ---------------------------------------------------------------------

class Switch:
    def __init__(self, csr_csv, ns="sw0"):
        self.bus = CommUDP(HOST_IP, 1234, csr_csv=csr_csv, timeout=5.0)
        with netns(ns):
            self.bus.open()
        self.regs = self.bus.regs

    def reg(self, name):
        return getattr(self.regs, name)

    def counters(self):
        c = {}
        for n in range(3):
            for name in ["rx_frames", "rx_drops", "filtered", "flooded"]:
                c[f"{name}{n}"] = self.reg(f"switch_ingress{n}_{name}").read()
            c[f"tx_frames{n}"] = self.reg(f"switch_egress{n}_tx_frames").read()
        return c

    def table(self, depth=256):
        entries = {}
        for i in range(depth):
            self.regs.switch_table_entry_index.write(i)
            info = self.regs.switch_table_entry_info.read()
            if info & 0b1:
                entries[self.regs.switch_table_entry_mac.read()] = (info >> 8) & 0b11
        return entries

# Tests --------------------------------------------------------------------------------------------

class Tests:
    def __init__(self, sim, csr_csv):
        self.sim     = sim
        self.csr_csv = csr_csv
        self.results = []

    def check(self, name, ok, info=""):
        self.results.append((name, ok))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f": {info}" if info else ""), flush=True)

    def run(self):
        sw = Switch(self.csr_csv)
        (ns0, _, ip0, mac0), (ns1, _, ip1, mac1) = NS

        # PHYs: RGMII in-band status (link up, 1Gbps, full-duplex) seen by both ECP5 RGMII PHYs.
        status = [sw.reg(f"ethphy{n}_rx_inband_status").read() for n in range(2)]
        self.check("RGMII in-band status: both links up, 1Gbps, full-duplex",
            status == [0b1101, 0b1101], f"{status}")

        # Switching between the two RGMII PHYs.
        self.check("ping sw0 -> sw1 (PHY0 -> PHY1)", ping(ns0, ip1, count=5) == 5)
        self.check("ping sw1 -> sw0 (PHY1 -> PHY0)", ping(ns1, ip0, count=5) == 5)
        self.check("ping sw0 -> sw1, 1514-byte frames", ping(ns0, ip1, count=3, size=1472) == 3)

        # Internal host, alternating between peers on both PHYs.
        ok = all(ping(ns, HOST_IP, count=2) == 2 for ns in [ns0, ns1, ns0, ns1])
        self.check("ping internal host from sw0/sw1 alternately", ok)

        # Learning.
        table = sw.table()
        expected = {mac0: 0, mac1: 1, HOST_MAC: 2}
        self.check("MAC table learned sw0@port0, sw1@port1, host@port2",
            all(table.get(m) == p for m, p in expected.items()),
            ", ".join(f"{m:012x}@{p}" for m, p in table.items()))

        # Lossless forwarding of a UDP burst, without flooding known unicast to the host port.
        n, size = 400, 1400
        with netns(ns1):
            rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            rx.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
            rx.bind((ip1, 5000))
        with netns(ns0):
            tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rx.settimeout(30)
        before = sw.counters()
        t0 = time.time()
        for i in range(n):
            tx.sendto(i.to_bytes(4, "big") + bytes((i + k) & 0xff for k in range(size - 4)), (ip1, 5000))
        received = []
        try:
            while len(received) < n:
                data = rx.recv(2048)
                seq  = int.from_bytes(data[:4], "big")
                ok   = data[4:] == bytes((seq + k) & 0xff for k in range(size - 4))
                received.append((seq, ok))
        except socket.timeout:
            pass
        elapsed = time.time() - t0
        after = sw.counters()
        delta = {k: after[k] - before[k] for k in after}
        self.check(f"UDP burst sw0 -> sw1: {n} x {size} bytes, in order and intact",
            [s for s, _ in received] == list(range(n)) and all(ok for _, ok in received),
            f"{len(received)}/{n} received in {elapsed:.1f}s wall time")
        # The host port only sees the Etherbone requests reading the counters (one per register).
        eb_requests = len(after)
        self.check("burst: no drops, not flooded to the host port",
            delta["rx_drops0"] == 0 and delta["flooded0"] == 0 and delta["tx_frames2"] <= eb_requests,
            f"port0 rx +{delta['rx_frames0']} drops +{delta['rx_drops0']} flooded +{delta['flooded0']}, "
            f"port1 tx +{delta['tx_frames1']}, port2 (host) tx +{delta['tx_frames2']}")
        rx.close()
        tx.close()

        # Port isolation: restrict port 0 to the host port.
        sw.regs.switch_ingress0_forward_mask.write(0b100)
        isolated = ping(ns0, ip1, count=2, timeout=2) == 0 and ping(ns0, HOST_IP, count=2) == 2
        sw.regs.switch_ingress0_forward_mask.write(0b111)
        restored = ping(ns0, ip1, count=2) == 2
        self.check("port isolation (forward_mask): sw0 reaches host only, then restored",
            isolated and restored)

        # Flush and relearn.
        sw.regs.switch_table_control.write(1)
        flushed = sw.table()
        # Etherbone itself relearns sw0 and the host.
        relearned_ok = ping(ns1, ip0, count=2) == 2
        relearned = sw.table()
        self.check("table flush, then relearn",
            mac1 not in flushed and relearned_ok and relearned.get(mac1) == 1,
            f"after flush: {len(flushed)} entries, after traffic: {len(relearned)}")

        # Aging: with sw1 silent, its entry expires; sw0 and the host keep refreshing theirs.
        period = sw.regs.switch_table_aging_period.read()
        sw.regs.switch_table_aging_period.write(64) # A sweep every 256*64 cycles: ~0.33ms at 50MHz.
        deadline = time.time() + 60
        while time.time() < deadline:
            table = sw.table()
            if mac1 not in table:
                break
        sw.regs.switch_table_aging_period.write(period)
        self.check("aging: idle sw1 entry expires, active ones stay",
            mac1 not in table and table.get(mac0) == 0 and table.get(HOST_MAC) == 2,
            ", ".join(f"{m:012x}@{p}" for m, p in table.items()))
        self.check("traffic after aging", ping(ns1, ip0, count=2) == 2)

        sw.bus.close()
        return all(ok for _, ok in self.results)

# Main ---------------------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Colorlight i9 3-port switch simulation system test.")
    parser.add_argument("--output-dir", default=os.path.join("build", "sim_switch"), help="Simulation build directory.")
    parser.add_argument("--no-build",   action="store_true", help="Reuse the existing simulation build.")
    args = parser.parse_args()

    if os.geteuid() != 0:
        sys.exit("Run as root: TAP interfaces and network namespaces need CAP_NET_ADMIN.")

    gateware_dir = os.path.abspath(os.path.join(args.output_dir, "gateware"))
    csr_csv      = os.path.abspath(os.path.join(args.output_dir, "csr.csv"))
    if not args.no_build:
        here = os.path.dirname(os.path.abspath(__file__))
        subprocess.run([sys.executable, os.path.join(here, "colorlight_i9_switch.py"),
            "--sim", "--no-run", "--output-dir", args.output_dir], check=True,
            stdout=subprocess.DEVNULL)

    sim = SwitchSim(gateware_dir, log=os.path.join(args.output_dir, "sim.log"))
    try:
        sim.start()
        ok = Tests(sim, csr_csv).run()
    finally:
        sim.stop()
    print("\nAll tests passed." if ok else "\nSome tests FAILED.")
    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    main()
