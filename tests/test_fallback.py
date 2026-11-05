"""Burn2Slot fallback detection tests that do not need hardware."""

import unittest

import flash
from tests.test_protocol import build_cfi_buffer


class B2SCartLink:
    """A cartridge that only answers the Burn2Slot scan wiring under test."""

    def __init__(self, sequence=0, bus=0):
        self.sequence = sequence
        self.bus = bus
        self.mode = "array"
        self.cart = bytearray(b"\x24\xff\xae\x51" + b"\x00" * 0xFC)
        self.flash_id = bytes([0x01, 0x00, 0x7E, 0x22, 0x00, 0x00, 0x00, 0x00])
        self.cfi = (build_cfi_buffer() + b"\x00" * 0x200)[:0x400]
        self.unlock = [
            (flash.B2S_SEQS[sequence][0] * 2, flash._b2s_out(bus, 0xAA)),
            (flash.B2S_SEQS[sequence][1] * 2, flash._b2s_out(bus, 0x55)),
            (flash.B2S_SEQS[sequence][2] * 2, flash._b2s_out(bus, 0x90)),
        ]
        self.writes = []
        self.reads = []

    def write_seq(self, pairs):
        pairs = [(address, value & 0xFFFF) for address, value in pairs]
        self.writes.extend(pairs)
        if any(address == 0 and (value & 0xFF) == 0xF0
               for address, value in pairs):
            self.mode = "array"
            return
        if pairs == [(0, 0x50), (0, 0xFF), (0xAA, 0x98)]:
            self.mode = "cfi"
            return
        if pairs == self.unlock:
            self.mode = "id"

    def read_words(self, address, count):
        self.reads.append((address, count))
        if self.mode == "id":
            data = self.flash_id[address:address + count * 4]
        elif self.mode == "cfi":
            data = self.cfi[address:address + count * 4]
        else:
            data = bytes(self.cart[address:address + count * 4])
        data = data.ljust(count * 4, b"\xFF")
        return [int.from_bytes(data[index:index + 4], "little")
                for index in range(0, len(data), 4)]


class B2SHelperTests(unittest.TestCase):
    def test_wiring_transforms_round_trip(self):
        for bus in (0, 1, 2, 3):
            for value in (0x00AA, 0x5555, 0x1234, 0xFFFF, 0x0000):
                self.assertEqual(
                    flash._b2s_in(bus, flash._b2s_out(bus, value)) & 0xFFFF,
                    value & 0xFFFF)
        # The 8-bit-doubled wirings return the same byte in both halves.
        for bus in (4, 5):
            for value in (0x00AA, 0x1234, 0x0000):
                doubled = ((value << 8) | value) & 0xFFFF
                self.assertEqual(
                    flash._b2s_in(bus, flash._b2s_out(bus, value)) & 0xFFFF,
                    doubled)

    def test_synthetic_amd_profile_is_writable(self):
        spec = flash._b2s_amd_spec(0, 0, None, bytes([1, 0, 0x7E, 0x22]))
        profile = flash.Profile(spec)
        self.assertTrue(profile.spec.get("synthetic"))
        self.assertEqual(profile.command_set, "AMD")
        self.assertTrue(profile.supports_write())
        self.assertEqual(profile.sector_map(0x18000), [(0, 0x10000),
                                                      (0x10000, 0x10000)])


class ChipEraseTests(unittest.TestCase):
    def test_chip_erase_runs_the_profile_commands_and_checks_blank(self):
        class ChipLink:
            def __init__(self):
                self.seq = []

            def write_seq(self, pairs):
                self.seq.extend(pairs)

            def read_words(self, address, count):
                return [0xFFFF] * count

        profile = flash.Profile({
            "type": "AGB", "names": ["synthetic chip erase"],
            "command_set": "INTEL",
            "commands": {
                "chip_erase": [[0, 0x10], [0, 0x11]],
                "chip_erase_wait_for": [[None, None, None], [None, None, None]],
            },
        })
        link = ChipLink()
        self.assertTrue(flash.chip_erase(link, profile))
        self.assertEqual(link.seq, [(0, 0x10), (0, 0x11)])


class FallbackDetectionTests(unittest.TestCase):
    def test_amd_scan_builds_a_synthetic_profile(self):
        link = B2SCartLink(sequence=0, bus=0)
        result = flash.detect(link, [], fallback=True)
        self.assertIsNotNone(result.profile)
        self.assertEqual(result.method, "Burn2Slot AMD scan")
        self.assertTrue(result.profile.spec.get("synthetic"))
        self.assertEqual(result.flash_id, link.flash_id[:4])
        self.assertEqual(result.cfi["size"], 1 << 25)
        self.assertEqual(result.profile.total_size(result.cfi), 1 << 25)

    def test_scan_stops_when_no_wiring_matches(self):
        link = B2SCartLink(sequence=0, bus=0)
        # Make every unlock return the ordinary ROM bytes instead of an ID.
        link.unlock = []
        result = flash.detect(link, [], fallback=True)
        self.assertIsNone(result.profile)


if __name__ == "__main__":
    unittest.main()
