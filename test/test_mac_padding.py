#
# This file is part of LiteEth.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

import unittest
import random

from migen import *

from liteeth.common import *
from liteeth.mac.padding import LiteEthMACPaddingInserter, LiteEthMACPaddingChecker

from test.test_stream import StreamPacket, stream_inserter, stream_collector

# Test MAC Padding Inserter ------------------------------------------------------------------------

class TestMACPaddingInserter(unittest.TestCase):
    padding = eth_min_frame_length - eth_fcs_length # 60 bytes.

    def run_inserter(self, dw, lengths, seed=42):
        prng    = random.Random(seed)
        packets = [StreamPacket([prng.randrange(256) for _ in range(l)]) for l in lengths]
        dut     = LiteEthMACPaddingInserter(dw, self.padding)
        recvd   = []
        run_simulation(dut, [
            stream_inserter(dut.sink, src=packets, seed=seed),
            stream_collector(dut.source, dest=recvd, expect_npackets=len(packets), seed=seed),
        ])
        self.assertEqual(len(recvd), len(packets))
        for sent, got in zip(packets, recvd):
            msg      = f"dw={dw} lengths={lengths} length={len(sent.data)}"
            # Every added byte is zero, including unused lanes in the final payload word.
            self.assertEqual(got.data[:len(sent.data)], sent.data, msg)
            self.assertEqual(len(got.data), max(len(sent.data), self.padding), msg)
            self.assertEqual(got.data[len(sent.data):], [0]*max(0, self.padding - len(sent.data)), msg)

    def test_lengths(self):
        for dw in [8, 16, 32, 64, 128, 256, 512]:
            for length in [1, 7, 42, 55, 56, 57, 58, 59, 60, 61, 64, 65, 100]:
                with self.subTest(dw=dw, length=length):
                    self.run_inserter(dw, [length])

    def test_short_frame_after_near_minimum_frame(self):
        # A frame of 57..59 bytes ends in the last padding word with a smaller be: the next
        # short frame must still be padded (regression: counter not reset, next frame sent as a
        # runt).
        for dw in [16, 32, 64, 128, 256, 512]:
            for length in [57, 58, 59]:
                for following in [1, 42, 46, 59]:
                    with self.subTest(dw=dw, length=length, following=following):
                        self.run_inserter(dw, [length, following, following, length, 42])

    def test_random_sequences(self):
        prng = random.Random(7)
        for dw in [8, 32, 64]:
            lengths = [prng.choice([prng.randrange(1, 70), prng.randrange(55, 61)])
                for _ in range(40)]
            with self.subTest(dw=dw):
                self.run_inserter(dw, lengths, seed=dw)

# Test MAC Padding Checker -------------------------------------------------------------------------

class TestMACPaddingChecker(unittest.TestCase):
    packet_min_length = eth_min_frame_length - eth_fcs_length # 60 bytes.

    def run_checker(self, dw, lengths):
        """Return, for each frame, whether the checker flagged it as a runt."""
        dut     = LiteEthMACPaddingChecker(dw, self.packet_min_length)
        nb      = dw//8
        flagged = []

        def generator():
            yield dut.source.ready.eq(1)
            for length in lengths:
                nwords = (length + nb - 1)//nb
                for i in range(nwords):
                    last = (i == nwords - 1)
                    yield dut.sink.valid.eq(1)
                    yield dut.sink.last.eq(last)
                    yield dut.sink.be.eq((1 << (length - i*nb if last else nb)) - 1)
                    yield
                    if last:
                        flagged.append((yield dut.source.error) != 0)
                yield dut.sink.valid.eq(0)
                yield

        run_simulation(dut, generator())
        return flagged

    def test_lengths(self):
        # Only frames shorter than the minimum are flagged, whatever their length otherwise
        # (regression: the length counter wrapped at 2048 bytes).
        lengths = [1, 59, 60, 61, 1530, 2047, 2048, 2049, 2107, 2108, 4096, 4100, 9000, 59]
        for dw in [8, 32, 64, 128]:
            with self.subTest(dw=dw):
                self.assertEqual(self.run_checker(dw, lengths),
                    [l < self.packet_min_length for l in lengths])

