# Ethernet Switch

`liteeth.switch` provides a transparent (unmanaged) layer-2 learning switch. Frames are forwarded
unmodified based on their destination MAC address. Ports can be PHYs, attached through regular
LiteEth MAC cores, or internal users such as the LiteEth UDP/IP stack or a CPU Wishbone MAC.

The reference design is a 3-port switch on the Colorlight i9's two RGMII PHYs, with an internal
host (ICMP + Etherbone) on the third port: [`bench/colorlight_i9_switch.py`](../bench/colorlight_i9_switch.py).

```
             +--------------------------------- LiteEthSwitch (sys) ---------------------------------+
 RGMII PHY0 -+- MAC core 0 -> Ingress 0 (store & forward) --+                     +-> Egress 0 -> MAC core 0 -+- RGMII PHY0
 RGMII PHY1 -+- MAC core 1 -> Ingress 1 (store & forward) --+--> Crossbar -------+-> Egress 1 -> MAC core 1 -+- RGMII PHY1
 Host       -+------------->  Ingress 2 (store & forward) --+    (fan-out)        +-> Egress 2 ---------------+- Host
             |                       |   ^                             ^                                     |
             |                       v   |                             |                                     |
             |                    MAC Table (learning, aging)     Allocator (atomic, round-robin)            |
             +---------------------------------------------------------------------------------------------+
```

## Behavior

- **Store and forward**: each ingress buffers complete frames in a `PacketDropFIFO`. Frames with
  errors (FCS/PHY errors flagged by the MAC core), runts (shorter than a MAC header) and frames
  overflowing the buffer are dropped before being forwarded, so errored frames never propagate and
  forwarded frames get a correct FCS from the egress MAC core.
- **Learning**: the source address of every good frame (re)binds to its ingress port. Group
  (multicast) source addresses are not learned.
- **Forwarding**: broadcast, multicast and unknown unicast are flooded to all other ports. Known
  unicast goes to its port only, and is filtered when that is the ingress port.
- **Ordering**: frames from one ingress port leave each egress port in order.
- **Aging**: a background sweeper visits one table entry every `aging_period` cycles. A visit
  clears an entry's age bit, or invalidates the entry when the bit was already clear; learning
  sets it. Idle addresses therefore expire within `aging_time` (default 300s).
- **Port isolation**: each port has a `forward_mask` of the ports it may forward to.

### Datapath and allocation

The datapath is `dw` bits wide in the `sys` clock domain: the bandwidth per port and direction is
`dw*sys_clk_freq`, minus a per-frame overhead (table lookup, allocation) of 5 cycles.

An ingress requests all its egress ports at once and an allocator grants them atomically, so a
flood is transmitted once, in lockstep, to every egress port. Grants never hold-and-wait, so
concurrent floods can't deadlock. Requests are served in round-robin order; the first waiting
requester reserves the egress ports it waits for, so floods can't be starved by unicast traffic,
while requests to other egress ports proceed in parallel.

### MAC table

The table is a direct-mapped hash table (`table_depth` entries, XOR-folded MAC address) in block
RAM, shared by all ports. Each frame costs one lookup and one learn, a few cycles. A new address
replaces whatever occupied its slot.

## Performance

Figures for the Colorlight i9 configuration: 3 ports, `dw=32`, `sys` at 50MHz, 1Gbps PHYs.

### Throughput and forwarding rate

| Metric                                           | Value                                              |
|--------------------------------------------------|----------------------------------------------------|
| Internal bandwidth                               | 1.6Gbps per port and direction (4.8Gbps in total)  |
| Cost of a 64-byte frame (unicast or flood)       | 20 cycles (15 data words + 5): 400ns, vs 672ns on the wire |
| Internal forwarding capacity                     | 2.5Mpps per port (7.5Mpps in total)                |
| MAC table                                        | 3 cycles per lookup/learn: 16.7M lookups/s, shared |
| Switching capacity (ports x 1Gbps x 2)           | 6Gbps (4Gbps for the two RGMII ports)              |
| Forwarding rate at line rate (64-byte frames)    | 1.488Mpps per port: 4.46Mpps (2.98Mpps for the RGMII ports) |

The per-frame overhead overlaps with egress transmission, so every port sustains 1Gbps in both
directions at once, whatever the frame size. `TestSwitchLineRate` in `test/test_switch.py` feeds
each ingress at exactly line rate (preamble, FCS and IFG included) and drains each egress like a
1Gbps wire, and checks that nothing is dropped, lost, reordered or corrupted for:

- 3-port permutation traffic (0 -> 1, 1 -> 2, 2 -> 0), 64-byte frames;
- a full-duplex 2-port bridge (0 <-> 1), 64-byte and 1518-byte frames;
- floods, 64-byte frames.

In these cases each ingress only ever holds the frame being stored and forwarded. Fed 30% faster
than its egress drains, the switch drops frames, as expected, and the test reports them.

The internal host port (LiteEth UDP/IP + Etherbone) is fed at line rate by the switch, but the
host itself processes far less: it is a management port.

### Congestion and latency

- Several ingresses towards one egress (e.g. 2Gbps into a 1Gbps port) are only absorbed by the
  4KiB ingress buffers (2.7 full-size frames, at most `param_depth` frames), then frames are
  dropped and counted in `ingress<n>_rx_drops`.
- Each ingress serves its frames in order: a frame waiting for a congested egress also delays the
  ones behind it for other egress ports (head-of-line blocking). For all ports saturated with
  uniformly random destinations, an input-queued switch with 3 ports is limited to about 68%
  throughput (theoretical figure, not measured here). Bridging and permutation traffic are not
  affected and run at 100%.
- Store and forward: latency grows with frame length, at least one frame time (0.5us for 64-byte,
  12us for 1518-byte frames) plus the pipeline.

### Maximum frame length

Measured on the simulated i9 (PHY0 -> switch -> PHY1, lengths without FCS):

| Frame length    | Result                                                                   |
|-----------------|--------------------------------------------------------------------------|
| 60 - 2047 bytes | Forwarded (covers 1518-byte and VLAN-tagged 1522-byte frames).            |
| 2048 - 2107     | Dropped: see below.                                                      |
| 2108 - 4095     | Forwarded.                                                               |
| 4096 and more   | Dropped: frames don't fit the 4KiB ingress buffer (4096 also hits the issue below). |

The 2048 - 2107 gap comes from LiteEth's MAC RX padding checker: its length counter is sized for
`eth_mtu` (1530 bytes, 11 bits) and wraps, flagging frames whose length modulo 2048 is under 60
bytes as runts. Such frames reach the switch intact (no CRC errors) and are dropped by the
ingress. The internal host (LiteEth UDP/IP stack) accepts frames up to `eth_mtu` (1530 bytes).

9000-byte jumbo frames would need `buffer_size=16384` (an estimated 11 block RAMs per ingress
instead of 3) and a wider length counter in the padding checker.

## Integration

```python
from liteeth.phy.ecp5rgmii import LiteEthPHYRGMII
from liteeth.frontend.etherbone import LiteEthEtherbone
from liteeth.switch import LiteEthSwitch, LiteEthSwitchUDPIPCore

# PHYs: each one in its own eth<n>_rx/eth<n>_tx clock domains.
self.ethphy0 = ClockDomainsRenamer({"eth_rx": "eth0_rx", "eth_tx": "eth0_tx"})(LiteEthPHYRGMII(...))
self.ethphy1 = ClockDomainsRenamer({"eth_rx": "eth1_rx", "eth_tx": "eth1_tx"})(LiteEthPHYRGMII(...))

# Switch: ports 0/1 on the PHYs (MAC cores with matching clock domain names).
self.switch = LiteEthSwitch(nports=3, dw=32, clk_freq=sys_clk_freq)
self.switch.add_phy(0, self.ethphy0, cd="eth0")
self.switch.add_phy(1, self.ethphy1, cd="eth1")

# Port 2: internal host with UDP/IP, ICMP and Etherbone.
self.ethcore = LiteEthSwitchUDPIPCore(mac_address=0x10e2d5000000, ip_address="192.168.1.50",
    clk_freq=sys_clk_freq, dw=32)
self.switch.connect(2, self.ethcore)
self.etherbone = LiteEthEtherbone(self.ethcore.udp, 1234)
self.bus.add_master(name="etherbone", master=self.etherbone.wishbone.bus)
```

`switch.connect(n, user)` accepts any user with `sink`/`source` endpoints carrying frames without
preamble/FCS (`eth_phy_description(dw)`), e.g. `LiteEthMACWishboneInterface` for a CPU.

| Parameter      | Default | Description                                                     |
|----------------|---------|-----------------------------------------------------------------|
| `nports`       | 3       | Number of ports.                                                |
| `dw`           | 32      | Datapath width (8, 16, 32 or 64).                               |
| `clk_freq`     | None    | `sys` frequency; sets the default aging period.                 |
| `table_depth`  | 256     | MAC table entries (power of two).                               |
| `aging_time`   | 300     | Aging time in seconds (0: aging disabled by default).           |
| `buffer_size`  | 4096    | Ingress buffer per port in bytes (power of two, >= one frame).  |
| `param_depth`  | 16      | Frames each ingress buffer can hold.                            |
| `egress_depth` | 8       | Egress FIFO depth in words.                                     |
| `with_csr`     | True    | Control/statistics CSRs.                                        |

### CSRs

| CSR                          | Description                                                       |
|------------------------------|-------------------------------------------------------------------|
| `table_control`              | `flush`: invalidate every entry.                                  |
| `table_aging_period`         | Cycles between two sweeper visits (0: aging disabled).            |
| `table_entry_index`          | Entry to read: a write latches it into `table_entry_*`.           |
| `table_entry_info`           | `valid`, `age`, `port` of the read entry.                         |
| `table_entry_mac`            | MAC address of the read entry.                                    |
| `table_hits`/`table_misses`  | Destination lookups that hit / missed.                            |
| `ingress<n>_forward_mask`    | Egress ports port `n` may forward to.                             |
| `ingress<n>_rx_frames`       | Frames received.                                                  |
| `ingress<n>_rx_drops`        | Frames dropped on reception (error, runt, buffer overflow).       |
| `ingress<n>_filtered`        | Frames filtered (destination on the ingress port, isolation).     |
| `ingress<n>_flooded`         | Frames flooded (broadcast, multicast, unknown unicast).           |
| `egress<n>_tx_frames`        | Frames transmitted.                                               |

## Colorlight i9

The i9's PHYs share their reset and MDIO pins: PHY0 owns them, PHY1 only gets its data pins. Both
use LiteEth's ECP5 RGMII PHY at 1Gbps, as litex-boards' `colorlight_i5` target does. The MAC cores
process preamble/FCS/padding in `sys` (`with_sys_datapath=True`, 50MHz) so that only width
conversion and (16-word) clock domain crossings run at the 125MHz RGMII clocks.

```sh
./bench/colorlight_i9_switch.py --build --load
ping 192.168.1.50                                    # Internal host, from either port.
litex_server --udp --udp-ip 192.168.1.50 &           # Then e.g. litex_cli --regs | grep switch
```

### Resources and timing

Built with yosys 0.69 / nextpnr-ecp5 0.11 for the LFE5U-45F-6, compared with single-port
Etherbone baselines: same CRG, LEDs and 50MHz `sys` clock, one ECP5 RGMII PHY and LiteX's
`add_etherbone()` (LiteEth MAC + UDP/IP + Etherbone, `buffer_depth=16`) with 8-bit (default) or
32-bit data width. Placement seed 1, plus seed 2 for the switch.

| Design                            | LUTs (`TRELLIS_COMB`) | FFs         | Block RAMs | RGMII clock (125MHz)  | `sys` (50MHz)       |
|-----------------------------------|-----------------------|-------------|------------|-----------------------|---------------------|
| `add_etherbone`, 8-bit (default)  | 5596 (13%)            | 3004 (7%)   | 1          | 66MHz: fails          | 81MHz               |
| `add_etherbone`, 32-bit           | 7443 (17%)            | 2875 (7%)   | 0          | 131MHz                | 49MHz: fails        |
| 3-port switch SoC                 | 13917 (32%)           | 5411 (12%)  | 11 (10%)   | 151-159MHz            | 53.6-55.6MHz        |

Compared with the 32-bit baseline, which has the same host stack width, the switch SoC uses 1.9x
the LUTs (+6.5k), 1.9x the FFs (+2.5k) and 11 more block RAMs; 2.5x the LUTs of the 8-bit
default. The switch core alone (`LiteEthSwitch`, 3 ports, without CSRs) synthesizes to 1990 LUT4,
128 CCU2C, 105 TRELLIS_DPR16X4, 1238 FFs and the 11 block RAMs: three 4KiB ingress buffers (3
each) and the 256-entry MAC table (2). The rest of the difference is the second PHY with its MAC
core (32-bit FCS generation and checking) and the statistics/control CSRs (17 32-bit counters).

Neither baseline meets timing as configured: with 8-bit data width, LiteX clocks the whole
UDP/IP + Etherbone stack from the 125MHz RGMII RX clock (critical path in the IP TX checksum and
crossbar); with 32-bit, the stack runs in `sys` and misses 50MHz by a small margin. The switch SoC
meets timing thanks to the MAC cores' `sys` datapath and 16-word buffered clock domain crossings.

## Simulation with TAP interfaces

`./bench/colorlight_i9_switch.py --sim` simulates the same SoC with Verilator. The ECP5 RGMII PHYs
are kept: their I/O primitives (DDR I/Os, `DELAYG`) are modeled and the `rgmii_ethernet` LiteX
simulation module ([`liteeth/phy/simulation`](../liteeth/phy/simulation)) plays each external PHY,
bridging its RGMII pads to a TAP interface (`tap0`, `tap1`): it generates the DDR RX stream with
preamble/FCS and in-band status (link up, 1Gbps, full-duplex), and checks the preamble/FCS of
transmitted frames. Set `RGMII_ETHERNET_DEBUG=1` to log frames.

[`bench/test_colorlight_i9_switch_sim.py`](../bench/test_colorlight_i9_switch_sim.py) builds and
runs the simulation, moves each TAP into its own network namespace, so that the host kernel can't
short-circuit traffic between them, and checks (as root):

```
sw0: tap0 192.168.1.100 -- PHY0 --+
                                  +-- switch -- host 192.168.1.50 (ICMP, Etherbone)
sw1: tap1 192.168.1.101 -- PHY1 --+
```

- RGMII in-band link status on both PHYs.
- Ping between the namespaces, both ways, with up to 1514-byte frames.
- Ping of the internal host from both namespaces, alternately.
- MAC table contents (read over Etherbone through the switch).
- A 400 x 1400-byte UDP burst between the namespaces: in order, intact, no drops and not flooded
  to the host port.
- Port isolation, table flush and relearning, aging.

Unit tests of the switch logic: `python3 -m unittest test.test_switch`.

## Limitations

- No VLANs, spanning tree or flow control (pause frames are forwarded like other multicast).
- Direct-mapped table: two active addresses hashing to the same slot evict each other, causing
  extra flooding.
- Frames longer than 4095 bytes, and frames of 2048 - 2107 bytes, are dropped (see
  [Maximum frame length](#maximum-frame-length)).
- Head-of-line blocking: an ingress waits for all the egress ports of its head frame. An internal
  port that stops accepting frames eventually stalls the floods towards it.
- PHY ports run at 1Gbps (the ECP5 RGMII PHY's `with_dynamic_link` would add 10/100Mbps; this
  is not exercised by the simulation).
