"""Delta detection tests: diff scan, state file, and CRC32 verification.

These use an in-memory cartridge instead of the link, so they run without
hardware and cover the paths that decide what gets erased and rewritten.
"""

import os
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

import flash
from flashgblink.gbalink import ST_ECHO

SECTOR = 0x20000
SECTORS = 8


class FakeProfile:
    primary_name = "fake 2006-36-71_V2"
    command_set = "INTEL"
    spec = {}
    commands = {}

    def total_size(self, cfi=None):
        return SECTOR * SECTORS

    def pad_to_sector(self, size, cfi=None):
        return (size + SECTOR - 1) // SECTOR * SECTOR

    def sector_map(self, size, cfi=None):
        return [[offset, SECTOR] for offset in range(0, size, SECTOR)]

    def buffer_size(self, cfi=None):
        return 64


class FakeCartLink:
    """An in-memory cartridge behind the word protocol."""

    def __init__(self, cart, crc=True):
        self.cart = bytearray(cart)
        self.crc_enabled = crc
        self.reads = []
        self.crcs = []
        self.blocks = []
        self.seq = []

    def read_words(self, address, count, timeout=10.0):
        self.reads.append((address, count * 4))
        data = bytes(self.cart[address:address + count * 4])
        data = data.ljust(count * 4, b"\xFF")
        return [int.from_bytes(data[index:index + 4], "little")
                for index in range(0, len(data), 4)]

    def crc32(self, address, length, timeout=10.0):
        if not self.crc_enabled:
            raise flash.UnsupportedCommand("old payload")
        self.crcs.append((address, length))
        return zlib.crc32(bytes(self.cart[address:address + length])) & 0xFFFFFFFF

    def write_seq(self, pairs):
        self.seq.extend(pairs)

    def boot(self):
        pass

    def close(self):
        pass


def program(link, address, data):
    link.blocks.append((address, bytes(data)))
    link.cart[address:address + len(data)] = data


def corrupting_program(link, address, data):
    link.blocks.append((address, bytes(data)))
    link.cart[address:address + len(data)] = b"\x00" * len(data)


class DeltaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.image = Path(self.temp.name) / "rom.gba"
        self.rom = bytearray(b"\xFF" * (SECTOR * SECTORS))
        self.rom[0:16] = b"DELTAROM\x00\x00\x00\x00\x00\x00\x00\x00"
        self.rom[SECTOR * 2 + 100] = 0x11
        self.rom[SECTOR * 3 + 200] = 0x33
        self.image.write_bytes(bytes(self.rom))
        patchers = [
            mock.patch.dict(os.environ, {
                "FLASHGBLINK_STATE_DIR": str(Path(self.temp.name) / "state")}),
            mock.patch.object(flash, "cart_reset",
                              lambda *args, **kwargs: None),
            mock.patch.object(flash, "progress",
                              lambda *args, **kwargs: None),
            mock.patch.object(flash, "done", lambda *args, **kwargs: None),
            mock.patch.object(flash, "plan_prepare_write",
                              lambda *args, **kwargs: (4096, 4096)),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.erase_patcher = mock.patch.object(
            flash, "sector_erase", self._erase)
        self.erase_patcher.start()
        self.addCleanup(self.erase_patcher.stop)
        self.write_patcher = mock.patch.object(
            flash, "write_block", program)
        self.write_patcher.start()
        self.addCleanup(self.write_patcher.stop)

    @staticmethod
    def _erase(link, address, profile=None, timeout=30.0):
        link.cart[address:address + SECTOR] = b"\xFF" * SECTOR
        return True

    def write(self, link, **kwargs):
        with mock.patch("sys.stdout"):
            flash.write_image(link, str(self.image), FakeProfile(), **kwargs)

    def state_files(self):
        root = Path(self.temp.name) / "state"
        return sorted(root.glob("*.json")) if root.is_dir() else []

    def test_first_run_reads_back_and_saves_a_state(self):
        link = FakeCartLink(b"\xFF" * (SECTOR * SECTORS))
        self.write(link)
        self.assertEqual(bytes(link.cart), bytes(self.rom))
        self.assertEqual(len(self.state_files()), 1)

    def test_unchanged_image_skips_the_scan_and_the_write(self):
        cart = bytearray(b"\xFF" * (SECTOR * SECTORS))
        link = FakeCartLink(cart)
        self.write(link)
        link.crcs.clear()
        link.blocks.clear()
        self.write(link)
        # Only the CRC capability probe; the state fast path needs no sectors.
        self.assertEqual(link.crcs, [(0, 4)])
        self.assertEqual(link.blocks, [])

    def test_change_outside_the_first_sector_is_repaired(self):
        link = FakeCartLink(b"\xFF" * (SECTOR * SECTORS))
        self.write(link)
        link.cart[SECTOR * 3 + 1] = 0x00
        link.blocks.clear()
        self.write(link)
        self.assertEqual(len(link.blocks), 1)
        self.assertEqual(link.blocks[0][0] // SECTOR, 3)
        self.assertEqual(bytes(link.cart), bytes(self.rom))

    def test_diff_scan_runs_on_the_payload_not_the_link(self):
        cart = bytearray(b"\xFF" * (SECTOR * SECTORS))
        link = FakeCartLink(cart)
        self.write(link)
        link.cart[SECTOR * 5 + 1] = 0x00
        link.reads.clear()
        link.crcs.clear()
        self.write(link)
        # One CRC per sector instead of reading 8 sectors over the link, plus
        # one CRC for the sector that is rewritten and verified.
        self.assertEqual([length for _address, length in link.crcs[:1 + SECTORS]],
                         [4] + [SECTOR] * SECTORS)
        self.assertFalse(any(length >= 4096 for _address, length in link.reads))

    def test_verification_does_not_transfer_the_sector(self):
        link = FakeCartLink(b"\xFF" * (SECTOR * SECTORS))
        self.write(link)
        self.assertFalse(any(length >= 4096 for _address, length in link.reads))

    def test_no_verify_leaves_no_state_and_does_not_hide_a_bad_write(self):
        link = FakeCartLink(b"\xFF" * (SECTOR * SECTORS))
        self.write_patcher.stop()
        bad = mock.patch.object(flash, "write_block", corrupting_program)
        bad.start()
        try:
            self.write(link, verify=False)
        finally:
            bad.stop()
            self.write_patcher.start()
        self.assertEqual(self.state_files(), [])
        link.blocks.clear()
        self.write(link)
        self.assertEqual(len(link.blocks), 3)
        self.assertEqual(bytes(link.cart), bytes(self.rom))

    def test_rejected_block_re_erases_the_sector(self):
        link = FakeCartLink(b"\xFF" * (SECTOR * SECTORS))
        calls = {"fail": 1}

        def flaky(link, address, data):
            if calls["fail"]:
                calls["fail"] -= 1
                raise flash.ProtocolError("payload rejected block write")
            program(link, address, data)

        self.write_patcher.stop()
        with mock.patch.object(flash, "write_block", flaky):
            self.write(link)
        self.write_patcher.start()
        self.assertEqual(calls["fail"], 0)
        self.assertEqual(bytes(link.cart), bytes(self.rom))
        self.assertEqual(len(self.state_files()), 1)

    def test_failed_verify_raises_and_leaves_no_state(self):
        link = FakeCartLink(b"\xFF" * (SECTOR * SECTORS))
        self.write_patcher.stop()
        stuck = mock.patch.object(flash, "write_block", lambda *args: None)
        stuck.start()
        try:
            with self.assertRaises(flash.ProtocolError):
                self.write(link)
        finally:
            stuck.stop()
            self.write_patcher.start()
        self.assertEqual(self.state_files(), [])

    def test_old_payload_falls_back_to_reading_the_cartridge(self):
        link = FakeCartLink(b"\xFF" * (SECTOR * SECTORS), crc=False)
        self.write(link)
        self.assertEqual(bytes(link.cart), bytes(self.rom))
        link.cart[SECTOR * 4 + 1] = 0x00
        link.reads.clear()
        link.blocks.clear()
        self.write(link)
        self.assertEqual(bytes(link.cart), bytes(self.rom))
        # No CRC command: the sector must be read back over the link instead.
        self.assertTrue(any(length >= 4096 for _address, length in link.reads))

    def test_state_paths_are_per_profile_and_sector_map(self):
        sectors = [(offset, SECTOR) for offset in range(0, SECTOR * SECTORS,
                                                        SECTOR)]
        with mock.patch.dict(os.environ, {
                "FLASHGBLINK_STATE_DIR": str(Path(self.temp.name) / "state")}):
            first = flash.default_state_path(FakeProfile(), sectors)

            class Other(FakeProfile):
                primary_name = "another profile"

            second = flash.default_state_path(Other(), sectors)
        self.assertNotEqual(first, second)
        self.assertEqual(first.parent, second.parent)
        self.assertEqual(first.suffix, ".json")


class CrcCommandTests(unittest.TestCase):
    def test_crc_job_then_poll_then_fetch(self):
        frames = []

        def xfer(words, timeout=10.0):
            frames.append(list(words))
            if len(words) == 3:
                return [flash.ST_OK, ST_ECHO, ST_ECHO]
            return [flash.ST_OK, 0x12345678]

        link = flash.Link.__new__(flash.Link)
        link.xfer_with_recovery = xfer
        link.ping = lambda timeout=0.05: flash.ST_OK
        self.assertEqual(link.crc32(0x4000, 4096), 0x12345678)
        self.assertEqual(frames[0][0], (flash.HDR_MAGIC << 24)
                         | (flash.OP_CRC32 << 8))
        self.assertEqual(frames[0][1:], [0x4000, 4096])
        self.assertEqual(frames[1][0], (flash.HDR_MAGIC << 24)
                         | (flash.OP_CRC_GET << 8))

    def test_the_result_is_not_read_before_the_job_finishes(self):
        events = []

        def xfer(words, timeout=10.0):
            events.append("frame")
            if len(words) == 3:
                return [flash.ST_OK, ST_ECHO, ST_ECHO]
            return [flash.ST_OK, 0xCAFEBABE]

        def ping(timeout=0.05):
            events.append("ping")
            if events.count("ping") < 3:
                raise flash.TransferError("busy")
            return flash.ST_OK

        link = flash.Link.__new__(flash.Link)
        link.xfer_with_recovery = xfer
        link.ping = ping
        self.assertEqual(link.crc32(0, 4096), 0xCAFEBABE)
        self.assertEqual(events, ["frame", "ping", "ping", "ping", "frame"])

    def test_out_of_frame_replies_resync_and_fail(self):
        calls = []
        link = flash.Link.__new__(flash.Link)
        link.xfer_with_recovery = lambda words, timeout=10.0: (
            [flash.ST_OK, 0, 0][:len(words)])
        link.resync = lambda attempts=3: calls.append("resync") or True
        with self.assertRaises(flash.ProtocolError):
            link.crc32(0, 4)
        self.assertEqual(calls, ["resync"])

    def test_rejected_job_means_the_payload_is_old(self):
        link = flash.Link.__new__(flash.Link)
        link.xfer_with_recovery = lambda words, timeout=10.0: (
            [flash.ST_OK, flash.ST_BAD, flash.ST_BAD][:len(words)])
        with self.assertRaises(flash.UnsupportedCommand):
            link.crc32(0, 4)


if __name__ == "__main__":
    unittest.main()
