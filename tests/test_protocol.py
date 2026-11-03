"""Protocol and CFI tests that do not need hardware."""

import struct
import tempfile
import unittest
from pathlib import Path

import flash


class RecordingLink(flash.Link):
    """Captures the words that would be clocked to the GBA."""

    def __init__(self, replies=None):
        self.sent = []
        self.replies = list(replies) if replies else []

    def xfer(self, words, timeout=5.0):
        self.sent.append(list(words))
        if self.replies:
            if isinstance(self.replies[0], list):
                return self.replies.pop(0)
            replies, self.replies = self.replies, []
            return replies
        return [flash.ST_OK] * len(words)


class FramingTests(unittest.TestCase):
    def test_words_are_packed_big_endian(self):
        self.assertEqual(flash._pack_words([0x5A400000]), b"\x5a\x40\x00\x00")
        self.assertEqual(flash._pack_words([0xA5A50000]), b"\xa5\xa5\x00\x00")

    def test_write_seq_frames_pairs_and_ping(self):
        link = RecordingLink([flash.ST_OK] * 4)
        link.write_seq([(0x1F00000, 0x60), (0x1F00000, 0xD0)])
        words = link.sent[0]
        self.assertEqual(words[0], (flash.HDR_MAGIC << 24) | (flash.OP_WRITE_SEQ << 8) | 2)
        self.assertEqual(words[1:5], [0x1F00000, 0x60, 0x1F00000, 0xD0])
        self.assertEqual(words[-1], flash.PING)

    def test_read_words_takes_data_after_three_header_replies(self):
        link = RecordingLink([0, 0, 0, 0x11111111, 0x22222222, flash.ST_OK])
        data = link.read_words(0x1234, 2)
        self.assertEqual(data, [0x11111111, 0x22222222])
        words = link.sent[0]
        self.assertEqual(words[:3], [(flash.HDR_MAGIC << 24) | (flash.OP_READ << 8), 0x1234, 2])
        self.assertEqual(words[3:5], [0, 0])
        self.assertEqual(words[-1], flash.PING)

    def test_bad_status_is_rejected(self):
        link = RecordingLink([flash.ST_BAD])
        with self.assertRaises(flash.ProtocolError):
            link.write_seq([(0, 0x50)])

    def test_ok_status_mask(self):
        flash._check_status(flash.ST_OK)
        with self.assertRaises(flash.ProtocolError):
            flash._check_status(flash.ST_BAD)
        with self.assertRaises(flash.ProtocolError):
            flash._check_status(0xDEADBEEF)

    def test_progress_frames_the_percent_and_pings(self):
        link = RecordingLink()
        flash.progress(link, 42)
        words = link.sent[0]
        self.assertEqual(words[0],
                         (flash.HDR_MAGIC << 24) | (flash.OP_PROGRESS << 8) | 42)
        self.assertEqual(words[-1], flash.PING)

    def test_done_frames_the_completion_opcode(self):
        link = RecordingLink()
        flash.done(link)
        words = link.sent[0]
        self.assertEqual(words[0],
                         (flash.HDR_MAGIC << 24) | (flash.OP_DONE << 8))
        self.assertEqual(words[-1], flash.PING)

    def test_progress_phase_selects_the_verify_bar(self):
        link = RecordingLink()
        flash.progress(link, 42, phase=1)
        words = link.sent[0]
        self.assertEqual(
            words[0],
            (flash.HDR_MAGIC << 24) | (1 << 16) | (flash.OP_PROGRESS << 8) | 42)

    def test_progress_clamps_to_the_bar(self):
        link = RecordingLink()
        flash.progress(link, 250)
        self.assertEqual(link.sent[0][0] & 0xFF, 100)


def build_cfi_buffer(swap_pins=(), size_log2=25, erase_regions=((0x10000, 255), (0x8000, 4))):
    buffer = bytearray(0x200)
    buffer[0x20], buffer[0x22], buffer[0x24] = 0x51, 0x52, 0x59  # "QRY"
    buffer[0x36] = 0x17      # 1.7 V
    buffer[0x38] = 0x20      # 2.0 V
    buffer[0x4E] = size_log2
    buffer[0x58] = len(erase_regions)
    for index, (block, count) in enumerate(erase_regions):
        base = 0x5A + index * 8
        buffer[base] = (count - 1) & 0xFF
        buffer[base + 2] = (count - 1) >> 8
        buffer[base + 4] = (block // 256) & 0xFF
        buffer[base + 6] = (block // 256) >> 8
    for pair in swap_pins:
        buffer = bytearray(flash.swap_bits(value, pair) for value in buffer)
    return bytes(buffer)


class CfiTests(unittest.TestCase):
    def test_qry_profile(self):
        info = flash.parse_cfi(build_cfi_buffer())
        self.assertEqual(info["pins"], [])
        self.assertEqual(info["size"], 1 << 25)
        self.assertEqual(info["regions"], [(0x10000, 255), (0x8000, 4)])

    def test_rqz_profile_detects_swapped_pins(self):
        cfi = build_cfi_buffer(swap_pins=[(0, 1)])
        self.assertEqual(cfi[0x20:0x21], b"R")
        info = flash.parse_cfi(cfi)
        self.assertEqual(info["pins"], [(0, 1)])
        self.assertEqual(info["size"], 1 << 25)

    def test_unknown_signature_is_rejected(self):
        with self.assertRaises(flash.ProtocolError):
            flash.parse_cfi(b"\x00" * 0x200)


class SaveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def path(self, name):
        return Path(self.tmp.name) / name

    def test_dump_reads_the_save_media_not_the_cartridge(self):
        link = RecordingLink()
        flash.save_dump(link, self.path("out.sav"), 8)
        words = link.sent[0]
        self.assertEqual(words[1], 0x0E000000)
        self.assertEqual(words[2], 8)            # byte count, not words
        self.assertEqual((words[0] >> 8) & 0xFF, flash.OP_READ8)
        self.assertEqual(self.path("out.sav").stat().st_size, 8)

    def test_dump_unpacks_four_bytes_per_reply_word(self):
        link = RecordingLink([[flash.ST_OK] * 3 + [0x04030201] + [flash.ST_OK]])
        data = flash.save_dump(link, self.path("out.sav"), 4)
        self.assertEqual(data, b"\x01\x02\x03\x04")

    def test_restore_writes_bytes_not_16_bit_lanes(self):
        source = self.path("in.sav")
        source.write_bytes(b"\x01\x02\x03\x04\x05")
        link = RecordingLink([flash.ST_OK] * 8)
        flash.save_restore(link, source, verify=False, show_progress=False)
        words = link.sent[0]
        self.assertEqual((words[0] >> 8) & 0xFF, flash.OP_WRITE8)
        self.assertEqual(words[1], 0x0E000000)
        self.assertEqual(words[2], 5)            # odd length is fine
        self.assertEqual(words[3:5], [0x04030201, 0x00000005])

    def test_restore_refuses_a_file_larger_than_the_save(self):
        source = self.path("big.sav")
        source.write_bytes(b"\x00" * 8)
        with self.assertRaises(flash.ProtocolError):
            flash.save_restore(RecordingLink(), source, size=4, verify=False,
                               show_progress=False)

    def test_restore_verify_reports_the_first_mismatch(self):
        source = self.path("in.sav")
        source.write_bytes(b"\x01\x02\x03\x04")
        # Replay zeros on the read-back so the comparison fails.
        link = RecordingLink([[flash.ST_OK] * 5, [flash.ST_OK],
                              [0, 0, 0, 0, flash.ST_OK]])
        with self.assertRaises(flash.ProtocolError) as caught:
            flash.save_restore(link, source, verify=True, show_progress=False)
        self.assertIn("at 0x0 (expected 01, got 00)", str(caught.exception))


if __name__ == "__main__":
    unittest.main()


class ReadRegionTests(unittest.TestCase):
    def test_long_reads_are_split_into_link_sized_commands(self):
        calls = []

        class Cart:
            def read_words(self, address, count):
                calls.append((address, count))
                return [0] * count

        data = flash.read_region(Cart(), 0x1000, 0x3000)
        self.assertEqual(len(data), 0x3000)
        self.assertEqual([count for _address, count in calls],
                         [1024, 1024, 1024])
        self.assertEqual([address for address, _count in calls],
                         [0x1000, 0x2000, 0x3000])
