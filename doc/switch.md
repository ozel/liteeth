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
`dw*sys_clk_freq`, minus a per-frame overhead (table lookup, allocation) of 5 cycles. With `dw=32`,
a minimum-size frame takes 20 cycles, unicast or flooded: 400ns at 50MHz, against 672ns on a
1Gbps wire (64 bytes + preamble + IFG), so the switch forwards faster than line rate even for
minimum-size frames (see `test_line_rate_*` in `test/test_switch.py`).

An ingress requests all its egress ports at once and an allocator grants them atomically, so a
flood is transmitted once, in lockstep, to every egress port. Grants never hold-and-wait, so
concurrent floods can't deadlock. Requests are served in round-robin order; the first waiting
requester reserves the egress ports it waits for, so floods can't be starved by unicast traffic,
while requests to other egress ports proceed in parallel.

### MAC table

The table is a direct-mapped hash table (`table_depth` entries, XOR-folded MAC address) in block
RAM, shared by all ports. Each frame costs one lookup and one learn, a few cycles. A new address
replaces whatever occupied its slot.

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
conversion and clock domain crossing run at the 125MHz RGMII clocks.

```sh
./bench/colorlight_i9_switch.py --build --load
ping 192.168.1.50                                    # Internal host, from either port.
litex_server --udp --udp-ip 192.168.1.50 &           # Then e.g. litex_cli --regs | grep switch
```

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
- Head-of-line blocking: an ingress waits for all the egress ports of its head frame. An internal
  port that stops accepting frames eventually stalls the floods towards it.
- PHY ports run at 1Gbps (the ECP5 RGMII PHY's `with_dynamic_link` would add 10/100Mbps; this
  is not exercised by the simulation).
