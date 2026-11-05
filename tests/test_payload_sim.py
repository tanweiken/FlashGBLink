"""Tests that run the real payload C on the host.

The harness in tests/sim compiles payload.c with hardware access replaced by a
plain cartridge/save array. It is skipped when no C compiler is available.
"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import flash

ROOT = Path(__file__).resolve().parent.parent
SIM = ROOT / "tests" / "sim" / "payload_sim.c"
PING = 0x5A5A5A5A
HDR = lambda op, n=0: (0x5A << 24) | (op << 8) | n


def roll_sum(words):
    total = 0
    for word in words:
        total = ((total << 1) | (total >> 31)) & 0xFFFFFFFF
        total ^= word
    return total


@unittest.skipUnless(shutil.which("cc"), "a C compiler is required")
class PayloadSimTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.binary = Path(cls.tmp.name) / "payload_sim"
        subprocess.run(
            ["cc", "-O1", "-DPAYLOAD_SIM", "-o", str(cls.binary), str(SIM)],
            check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_frames(self, frames):
        text = "\n".join(" ".join("%08X" % word for word in frame)
                         for frame in frames) + "\n"
        result = subprocess.run([str(self.binary)], input=text,
                                text=True, capture_output=True, check=True)
        return [[int(word, 16) for word in line.split()]
                for line in result.stdout.splitlines()]

    def test_caps_reply_reports_checked_reads(self):
        out = self.run_frames([(HDR(flash.OP_CAPS), 0xFFFFFFFF, PING)])
        self.assertEqual(out[0][0], flash.ST_OK)
        self.assertEqual(out[0][1], (flash.CAPS_VERSION << 16)
                         | flash.CAP_READ_CHECK)
        self.assertEqual(out[0][2], flash.ST_OK)

    def test_checked_word_read_returns_data_and_checksum(self):
        frames = [
            (HDR(flash.OP_WRITE_SEQ, 2), 0x10, 0x0201, 0x12, 0x0403, PING),
            (HDR(flash.OP_READ_CHECK), 0x10, 1, 0, 0, PING),
        ]
        out = self.run_frames(frames)
        read = out[1]
        self.assertEqual(read[3], 0x04030201)
        self.assertEqual(read[4], roll_sum([0x04030201]))
        self.assertEqual(read[5], flash.ST_OK)

    def test_checked_byte_read_packs_the_save_bus(self):
        frames = [
            (HDR(flash.OP_WRITE8), 0x0E000010, 4, 0x04030201, PING),
            (HDR(flash.OP_READ8_CHECK), 0x0E000010, 4, 0, 0, PING),
        ]
        out = self.run_frames(frames)
        read = out[1]
        self.assertEqual(read[3], 0x04030201)
        self.assertEqual(read[4], roll_sum([0x04030201]))
        self.assertEqual(read[5], flash.ST_OK)

    def test_write_plan_programs_the_cartridge(self):
        # One command: write the program-data word at the block address.
        plan = [HDR(flash.OP_SET_PLAN), 64, 1,
                (flash.ADDR_PA << 30),
                (flash.VAL_PD << 30),
                0, 0, PING]
        # The block protocol packs two little-endian 16-bit words per word.
        data = [0x04030201, 0x08070605, 0x0C0B0A09, 0x100F0E0D] \
            + [0xFFFFFFFF] * 12
        block = [HDR(flash.OP_WRITE_BLOCK), 0x100, len(data)] + data + [PING]
        read = [HDR(flash.OP_READ_CHECK), 0x100, 8] + [0] * 8 + [0, PING]
        out = self.run_frames([plan, block, read])
        self.assertEqual(out[2][3], 0x04030201)

    def test_abort_clears_the_installed_plan(self):
        plan = [HDR(flash.OP_SET_PLAN), 64, 1,
                (flash.ADDR_PA << 30),
                (flash.VAL_PD << 30),
                0, 0, PING]
        abort = [HDR(flash.OP_ABORT), PING]
        # No data words: the handler rejects before consuming them, and the
        # trailing PING reports the ST_BAD that the host would poll.
        block = [HDR(flash.OP_WRITE_BLOCK), 0x100, 16, PING]
        out = self.run_frames([plan, abort, block])
        self.assertEqual(out[2][3], flash.ST_BAD)

    def test_progress_after_done_reinitialises_the_screen(self):
        # A second flash must not leave the completion screen behind.
        source = ROOT / "tests" / "sim" / "screen_reset.c"
        binary = Path(self.tmp.name) / "screen_reset"
        subprocess.run(
            ["cc", "-O1", "-I", str(ROOT), "-o", str(binary), str(source)],
            check=True, capture_output=True)
        result = subprocess.run([str(binary)], capture_output=True)
        self.assertEqual(result.returncode, 0,
                         result.stdout.decode() + result.stderr.decode())

    def test_amd_data_polling_uses_the_last_loaded_word(self):
        # AMD data polling is only valid at the last word loaded into the
        # write buffer, not at the profile's SA/PA wait address.  This
        # compiles the real payload against a small polling model.
        source = ROOT / "tests" / "sim" / "amd_wait.c"
        binary = Path(self.tmp.name) / "amd_wait"
        subprocess.run(
            ["cc", "-O1", "-I", str(ROOT), "-o", str(binary), str(source)],
            check=True, capture_output=True)
        result = subprocess.run([str(binary)], capture_output=True)
        self.assertEqual(result.returncode, 0,
                         result.stdout.decode() + result.stderr.decode())


if __name__ == "__main__":
    unittest.main()
