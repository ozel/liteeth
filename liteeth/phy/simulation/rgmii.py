#
# This file is part of LiteEth.
#
# Copyright (c) 2026 Oliver Keller <oliver.keller@pm.me>
# SPDX-License-Identifier: BSD-2-Clause

"""RGMII simulation support: run a real RGMII PHY (e.g. the ECP5 one) against a host TAP interface.

The ``rgmii_ethernet`` LiteX simulation module plays the external PHY on the RGMII pads. Clock each
PHY with its own ``clocker`` (125MHz) and keep the simulation timebase below 4ns (half an RGMII
clock period), so pad updates never coincide with the RGMII clock edges they are sampled on.
"""

import os
import shutil

from litex.build.generic_platform import Pins, Subsignal

sim_dir           = os.path.dirname(os.path.abspath(__file__))
sim_modules_dir   = os.path.join(sim_dir, "modules")
ecp5_sim_verilog  = os.path.join(sim_dir, "verilog", "ecp5_sim.v")

# IOs ----------------------------------------------------------------------------------------------

def rgmii_sim_ios(n):
    """Simulation IOs of RGMII PHY ``n``: its RX clock (driven by a clocker) and RGMII pads."""
    return [
        (f"eth{n}_rx_clk", 0, Pins(1)),
        ("eth", n,
            Subsignal("tx_clk",  Pins(1)),
            Subsignal("rx_ctl",  Pins(1)),
            Subsignal("rx_data", Pins(4)),
            Subsignal("tx_ctl",  Pins(1)),
            Subsignal("tx_data", Pins(4)),
        ),
    ]

def add_rgmii_sim_sources(platform):
    """Add Verilog models of the I/O primitives of the ECP5 RGMII PHY (DDR I/Os, DELAYG)."""
    platform.add_source(ecp5_sim_verilog)

class RGMIISimClockPads:
    """``clock_pads`` for an RGMII PHY in simulation."""
    def __init__(self, platform, n, pads):
        self.rx = platform.request(f"eth{n}_rx_clk")
        self.tx = pads.tx_clk

# Simulation config --------------------------------------------------------------------------------

def add_rgmii_sim_module(sim_config, n, interface, ip=None, mac=None, phase_deg=0):
    """Clock RGMII PHY ``n`` and bridge it to TAP ``interface``."""
    sim_config.add_clocker(f"eth{n}_rx_clk", freq_hz=125e6, phase_deg=phase_deg)
    args = {"interface": interface}
    if ip is not None:
        args["ip"] = ip
    if mac is not None:
        args["mac"] = mac
    sim_config.add_module("rgmii_ethernet", ("eth", n), clocks=f"eth{n}_rx_clk", args=args)

def rgmii_sim_extra_mods(build_dir):
    """Copy the module sources into the build directory (LiteX writes build files next to them).

    Returns the ``extra_mods``/``extra_mods_path`` arguments of the simulation build."""
    path = os.path.abspath(os.path.join(build_dir, "sim_modules"))
    shutil.copytree(os.path.join(sim_modules_dir, "rgmii_ethernet"),
        os.path.join(path, "rgmii_ethernet"), dirs_exist_ok=True)
    return dict(extra_mods=["rgmii_ethernet"], extra_mods_path=path)
