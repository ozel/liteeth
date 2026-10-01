#!/usr/bin/env python3

#
# This file is part of LiteEth.
#
# Copyright (c) 2026 Oliver Keller <oliver.keller@pm.me>
# SPDX-License-Identifier: BSD-2-Clause

"""3-port transparent Gigabit Ethernet switch on the Colorlight i9 dual RGMII PHYs.

Ports 0/1 are the two Broadcom B50612D RGMII PHYs (ECP5 RGMII PHY + LiteEth MAC cores), port 2 is
an internal host running the LiteEth UDP/IP stack with Etherbone (CSR access, incl. switch
statistics and MAC table, e.g. with ``litex_server --udp``) and ICMP echo.

Hardware:
    ./colorlight_i9_switch.py --build [--load]

Simulation (Verilator, same SoC with each RGMII PHY bridged to a TAP interface, run as root):
    ./colorlight_i9_switch.py --sim
"""

import os
import argparse
import subprocess

from migen import *

from litex.gen import *

from litex.build.generic_platform import Pins, Subsignal

from litex.soc.integration.soc_core import SoCMini
from litex.soc.integration.builder import Builder

from liteeth.phy.ecp5rgmii import LiteEthPHYRGMII
from liteeth.frontend.etherbone import LiteEthEtherbone
from liteeth.switch import LiteEthSwitch, LiteEthSwitchUDPIPCore

# Switch SoC ---------------------------------------------------------------------------------------

class SwitchSoC(SoCMini):
    """SoC around a 3-port switch: two RGMII PHYs and an internal Etherbone host."""
    def __init__(self, platform, sys_clk_freq, rgmii_pads,
        ident        = "LiteEth 3-port switch",
        host_mac     = 0x10e2d5000000,
        host_ip      = "192.168.1.50",
        udp_port     = 1234,
        table_depth  = 256,
        aging_time   = 300,
        **phy_kwargs):
        SoCMini.__init__(self, platform, clk_freq=sys_clk_freq, ident=ident, ident_version=True)

        # RGMII PHYs -------------------------------------------------------------------------------
        # Each PHY gets its own eth<n>_rx/eth<n>_tx clock domains.
        phys = []
        for n, (clock_pads, pads) in enumerate(rgmii_pads):
            phy = LiteEthPHYRGMII(clock_pads=clock_pads, pads=pads, **phy_kwargs)
            phy = ClockDomainsRenamer({"eth_rx": f"eth{n}_rx", "eth_tx": f"eth{n}_tx"})(phy)
            self.add_module(name=f"ethphy{n}", module=phy)
            phys.append(phy)

        # Switch -----------------------------------------------------------------------------------
        self.switch = switch = LiteEthSwitch(nports=3, dw=32, clk_freq=sys_clk_freq,
            table_depth = table_depth,
            aging_time  = aging_time,
        )
        for n, phy in enumerate(phys):
            # Preamble/FCS/padding in sys: only width conversion and CDC run at 125MHz. 16-word
            # CDCs are plenty (32-bit words at 50MHz vs 8-bit at 125MHz) and ease 125MHz timing.
            switch.add_phy(n, phy, cd=f"eth{n}",
                with_sys_datapath = True,
                tx_cdc_depth      = 16,
                rx_cdc_depth      = 16,
            )

        # Internal Host: UDP/IP + ICMP + Etherbone -------------------------------------------------
        self.ethcore = ethcore = LiteEthSwitchUDPIPCore(
            mac_address = host_mac,
            ip_address  = host_ip,
            clk_freq    = sys_clk_freq,
            dw          = 32,
        )
        switch.connect(2, ethcore)
        self.etherbone = LiteEthEtherbone(ethcore.udp, udp_port, buffer_depth=16)
        self.bus.add_master(name="etherbone", master=self.etherbone.wishbone.bus)
        self.phys = phys

# Colorlight i9 ------------------------------------------------------------------------------------

def colorlight_i9_rgmii_pads(platform):
    """RGMII pads of both PHYs. They share reset and MDIO: PHY0 owns them, PHY1 gets data pins only."""
    shared = {"rst_n", "mdio", "mdc"}
    for resource in platform.constraint_manager.available:
        if resource[:2] == ("eth", 1):
            items = [i for i in resource[2:] if not (isinstance(i, Subsignal) and i.name in shared)]
            platform.add_extension([("eth_data", 1, *items)])
            break
    return [
        (platform.request("eth_clocks", 0), platform.request("eth",      0)),
        (platform.request("eth_clocks", 1), platform.request("eth_data", 1)),
    ]

class ColorlightI9SwitchSoC(SwitchSoC):
    def __init__(self, revision="7.2", sys_clk_freq=50e6, **kwargs):
        from litex_boards.platforms import colorlight_i5
        from litex_boards.targets.colorlight_i5 import _CRG
        from litex.soc.cores.led import LedChaser

        platform = colorlight_i5.Platform(board="i9", revision=revision, toolchain="trellis")
        self.crg = _CRG(platform, sys_clk_freq)
        SwitchSoC.__init__(self, platform, sys_clk_freq,
            rgmii_pads = colorlight_i9_rgmii_pads(platform),
            ident      = "LiteEth 3-port switch on Colorlight i9",
            tx_delay   = 0e-9, # As litex-boards' colorlight_i5 target.
            **kwargs)

        # Timing constraints (RX clocks are constrained by the platform).
        for phy in self.phys:
            platform.add_false_path_constraints(self.crg.cd_sys.clk, phy.crg.cd_eth_rx.clk)

        # Leds.
        self.leds = LedChaser(pads=platform.request_all("user_led_n"), sys_clk_freq=sys_clk_freq)

# Simulation ---------------------------------------------------------------------------------------

class SimSwitchSoC(SwitchSoC):
    """Simulation of the Colorlight i9 switch SoC: same PHYs (ECP5 RGMII, I/O primitives modeled)."""
    def __init__(self, sys_clk_freq=50e6, **kwargs):
        from litex.build.sim import SimPlatform
        from litex.build.io import CRG
        from liteeth.phy.simulation.rgmii import rgmii_sim_ios, RGMIISimClockPads, add_rgmii_sim_sources

        io = [("sys_clk", 0, Pins(1)), ("sys_rst", 0, Pins(1))]
        for n in range(2):
            io += rgmii_sim_ios(n)
        platform = SimPlatform("SIM", io)
        add_rgmii_sim_sources(platform)
        self.crg = CRG(platform.request("sys_clk"))

        rgmii_pads = []
        for n in range(2):
            pads = platform.request("eth", n)
            rgmii_pads.append((RGMIISimClockPads(platform, n, pads), pads))
        SwitchSoC.__init__(self, platform, sys_clk_freq,
            rgmii_pads = rgmii_pads,
            ident      = "LiteEth 3-port switch simulation (Colorlight i9 PHYs)",
            **kwargs)

def sim_main(args):
    from litex.build.sim.config import SimConfig
    from liteeth.phy.simulation.rgmii import add_rgmii_sim_module, rgmii_sim_extra_mods

    sys_clk_freq = 50e6
    sim_config   = SimConfig()
    sim_config.add_clocker("sys_clk", freq_hz=sys_clk_freq)
    for n, tap in enumerate(args.taps):
        # PHY1's clock is shifted by half a period: the PHYs are not synchronous to each other.
        add_rgmii_sim_module(sim_config, n, tap, phase_deg=180*n)
    assert sim_config.get_timebase_ps() < 4000, "Timebase must be below half an RGMII clock period."

    soc = SimSwitchSoC(sys_clk_freq=sys_clk_freq, host_ip=args.host_ip, aging_time=args.aging_time)
    builder = Builder(soc, output_dir=args.output_dir, csr_csv=os.path.join(args.output_dir, "csr.csv"))
    builder.build(
        sim_config  = sim_config,
        interactive = False,
        opt_level   = "O3",
        threads     = args.threads,
        run         = not args.no_run,
        trace       = args.trace,
        **rgmii_sim_extra_mods(builder.gateware_dir),
    )
    if args.no_run:
        # LiteX only compiles when running: compile now, run later with obj_dir/Vsim (as root).
        with open(os.path.join(builder.gateware_dir, "build_sim.log"), "w") as log:
            subprocess.run(["bash", "build_sim.sh"], cwd=builder.gateware_dir, check=True,
                stdout=log, stderr=subprocess.STDOUT)
        print("Simulation built: {}".format(os.path.join(builder.gateware_dir, "obj_dir", "Vsim")))

# Main ---------------------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="LiteEth 3-port switch on Colorlight i9 dual RGMII PHYs.")
    parser.add_argument("--build",      action="store_true",       help="Build bitstream.")
    parser.add_argument("--load",       action="store_true",       help="Load bitstream.")
    parser.add_argument("--revision",   default="7.2",             help="Colorlight i9 revision.")
    parser.add_argument("--sim",        action="store_true",       help="Simulate with Verilator, PHYs bridged to TAPs.")
    parser.add_argument("--taps",       default="tap0,tap1",       help="Simulation TAP interfaces of PHY0,PHY1.")
    parser.add_argument("--no-run",     action="store_true",       help="Build the simulation without running it.")
    parser.add_argument("--threads",    default=1, type=int,       help="Verilator threads.")
    parser.add_argument("--trace",      action="store_true",       help="Enable simulation tracing.")
    parser.add_argument("--host-ip",    default="192.168.1.50",    help="Internal host IP address.")
    parser.add_argument("--aging-time", default=300, type=float,   help="MAC table aging time (s).")
    parser.add_argument("--output-dir", default=None,              help="Build directory.")
    args = parser.parse_args()
    args.taps = args.taps.split(",")
    assert len(args.taps) == 2

    if args.sim:
        args.output_dir = args.output_dir or os.path.join("build", "sim_switch")
        sim_main(args)
        return

    args.output_dir = args.output_dir or os.path.join("build", "colorlight_i9_switch")
    soc = ColorlightI9SwitchSoC(revision=args.revision, host_ip=args.host_ip, aging_time=args.aging_time)
    builder = Builder(soc, output_dir=args.output_dir, csr_csv=os.path.join(args.output_dir, "csr.csv"))
    builder.build(run=args.build)

    if args.load:
        prog = soc.platform.create_programmer()
        prog.load_bitstream(builder.get_bitstream_filename(mode="sram"))

if __name__ == "__main__":
    main()
