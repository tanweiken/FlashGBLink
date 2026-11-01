"""FlashGBX profile loading, write plans, and autodetection tests."""

import json
import tempfile
import unittest
from pathlib import Path

import flash
import flashgbx
from tests.test_protocol import RecordingLink, build_cfi_buffer


FIXTURES = Path(__file__).resolve().parent / "fixtures"
M36 = FIXTURES / "fc_AGB_M36L0R806.txt"
GL256S = FIXTURES / "fc_AGB_GL256S.txt"


class ProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.profiles = flashgbx.load_profiles(FIXTURES)
        cls.m36 = flashgbx.load_profile(M36)
        cls.gl256s = flashgbx.load_profile(GL256S)

    def test_loads_the_test_profiles(self):
        self.assertEqual(len(self.profiles), 4)

    def test_finds_a_profile_by_any_alias(self):
        found = flashgbx.find_profile(
            self.profiles, "2006-36-71_V2 with M36L0R8060B")
        self.assertIsNotNone(found)
        self.assertTrue(found.matches_name("36L0R8-39VF512 with M36L0R8060B"))

    def test_hex_literals_are_converted(self):
        self.assertEqual(self.m36.command_set, "INTEL")
        self.assertEqual(self.m36.flash_size, 0x2000000)
        self.assertIn([0x20, 0x00, 0x0E, 0x88], self.m36.flash_ids)
        self.assertEqual(self.m36.command("read_cfi"), [[0xAA, 0x98]])

    def test_profile_without_id_is_not_autodetected(self):
        generic = flashgbx.load_profile(
            FIXTURES / "fc_AGB_Generic_AAA_AA.txt")
        self.assertFalse(generic.is_autodetectable())

    def test_id_offsets_select_bytes_from_the_query(self):
        text = json.dumps({
            "type": "AGB",
            "names": ["offset test"],
            "flash_ids": [[0xC1, 0x7D, 0x22]],
            "flash_id_offsets": [0, 2, 3],
            "command_set": "AMD",
            "commands": {"read_identifier": [[0xAAA, 0xA9]]},
        })
        profile = flashgbx.parse_profile_text(text)
        self.assertEqual(profile.select_id_bytes(b"\xC1\x00\x7D\x22"),
                         b"\xC1\x7D\x22")

    def test_reset_relocation_only_moves_the_first_address(self):
        commands = [[0, 0x50], [0, 0xFF], [0xAAA, 0xAA]]
        moved = flashgbx.relocate_commands(commands, 0x1F00000)
        self.assertEqual(moved, [[0x1F00000, 0x50], [0x1F00000, 0xFF],
                                [0xAAA, 0xAA]])


class WritePlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m36 = flashgbx.load_profile(M36)
        cls.gl256s = flashgbx.load_profile(GL256S)

    def test_intel_buffer_plan_places_data_at_pd(self):
        chunk, words = flash.encode_write_plan(self.m36, 64)
        self.assertEqual(chunk, 64)
        self.assertEqual(len(words), 5 * 4)
        decode = lambda value: (value >> 30, value & 0x3FFFFFFF)
        self.assertEqual(decode(words[0]), (flash.ADDR_SA, 0))
        self.assertEqual(words[1] >> 30, flash.VAL_CONST)
        self.assertEqual(words[1] & 0xFFFF, 0xE8)
        self.assertEqual(words[5] >> 30, flash.VAL_BS)       # SA, BS
        self.assertEqual(words[8] >> 30, flash.ADDR_PA)      # PA, PD
        self.assertEqual(words[9] >> 30, flash.VAL_PD)
        self.assertEqual(words[13] & 0xFFFF, 0xD0)
        self.assertEqual(words[14] >> 30, flash.WAIT_SA)     # wait after D0
        self.assertEqual(words[15] >> 16, 0x80)

    def test_amd_buffer_plan_keeps_unlock_addresses_absolute(self):
        chunk, words = flash.encode_write_plan(self.gl256s, 32)
        self.assertEqual(chunk, 32)
        self.assertEqual(words[0] >> 30, flash.ADDR_ABS)
        self.assertEqual(words[0] & 0x3FFFFFFF, 0xAAA)
        self.assertEqual(words[1] & 0xFFFF, 0xA9)
        self.assertEqual(words[4] & 0x3FFFFFFF, 0x555)
        self.assertEqual(words[5] & 0xFFFF, 0x56)
        self.assertEqual(words[8] >> 30, flash.ADDR_SA)
        self.assertEqual(words[9] & 0xFFFF, 0x26)
        # The wait after 0x2A compares the read-back value with the last
        # programmed word (PD), which the payload substitutes.
        self.assertEqual(words[22] >> 30, flash.WAIT_PD)

    def test_profile_without_buffer_write_falls_back_to_single_words(self):
        profile = flashgbx.load_profile(
            FIXTURES / "fc_AGB_29GL128F.txt")
        chunk, words = flash.encode_write_plan(profile, None)
        self.assertEqual(chunk, 2)
        # AMD single write: unlock, 0xA0, PA/PD.
        self.assertEqual(len(words), 4 * 4)
        self.assertEqual(words[12] >> 30, flash.ADDR_PA)
        self.assertEqual(words[13] >> 30, flash.VAL_PD)
        # With no wait row, the plan polls the programmed address for PD.
        self.assertEqual(words[14] >> 30, flash.WAIT_PD)

    def test_numeric_wait_address_is_an_offset_from_the_word(self):
        chunk, words = flash.encode_write_plan(self.m36, None)
        self.assertEqual(chunk, 2)
        # Intel single write waits at [0, 0x80, 0x80]: address 0 is the
        # programmed word, not cartridge address zero.
        self.assertEqual(words[10] >> 30, flash.WAIT_SA)
        self.assertEqual(words[10] & 0x3FFFFFFF, 0)

    def test_prepare_write_installs_the_plan(self):
        cfi = flashgbx.parse_cfi(build_cfi_buffer())
        cfi["buffer_size"] = 64
        link = RecordingLink()
        block_bytes, chunk = flash.prepare_write(link, self.m36, cfi)
        self.assertEqual(chunk, 64)
        self.assertEqual(block_bytes % 64, 0)
        header = link.sent[0][0]
        self.assertEqual((header >> 8) & 0xFF, flash.OP_SET_PLAN)
        self.assertEqual(link.sent[0][1], 64)
        self.assertEqual(link.sent[0][2], 5)


class SectorMapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m36 = flashgbx.load_profile(M36)

    def test_cfi_sector_map_and_padding(self):
        cfi = flashgbx.parse_cfi(
            build_cfi_buffer(erase_regions=((0x10000, 255), (0x8000, 4))))
        sectors = self.m36.sector_map(0x400000, cfi)
        self.assertEqual(len(sectors), 64)
        self.assertEqual(sectors[0], (0, 0x10000))
        self.assertEqual(sectors[-1], (0x3F0000, 0x10000))
        self.assertEqual(self.m36.pad_to_sector(0x3F0001, cfi), 0x400000)

    def test_cfi_buffer_size_is_decoded(self):
        buffer = bytearray(build_cfi_buffer())
        buffer[0x54] = 6      # 64 bytes
        info = flashgbx.parse_cfi(bytes(buffer))
        self.assertEqual(info["buffer_size"], 64)

    def test_reversed_sector_table_is_used_for_the_map(self):
        buffer = bytearray(build_cfi_buffer(
            erase_regions=((0x8000, 4), (0x10000, 255))))
        buffer[0x2A] = 0x40      # primary query table at 0x80
        buffer[0x80:0x81] = b"P"
        buffer[0x82:0x83] = b"R"
        buffer[0x84:0x85] = b"I"
        buffer[0x80 + 0x1E] = 0x03
        info = flashgbx.parse_cfi(bytes(buffer))
        self.assertEqual(info["boot_sector_raw"], 0x03)
        self.assertEqual(flashgbx.cfi_sector_regions(info),
                         [(0x10000, 255), (0x8000, 4)])


class FakeLink:
    """A cartridge that answers identification and CFI queries."""

    def __init__(self, rom, flash_id, cfi=None):
        self.rom = bytes(rom)
        self.flash_id = bytes(flash_id)
        self.cfi = bytes(cfi or b"")
        self.mode = "rom"
        self.sent = []

    def write_seq(self, pairs, timeout=5.0):
        pairs = [tuple(pair) for pair in pairs]
        self.sent.append(pairs)
        if pairs == [(0, 0x90)]:
            self.mode = "id"
        elif pairs == [(0xAA, 0x98)]:
            self.mode = "cfi"
        elif pairs == [(0, 0x50), (0, 0xFF)]:
            self.mode = "rom"

    def read_words(self, address, count, timeout=10.0):
        if self.mode == "id":
            data = self.flash_id
        elif self.mode == "cfi":
            data = self.cfi
        else:
            data = self.rom
        raw = data[address:address + count * 4].ljust(count * 4, b"\xFF")
        return [int.from_bytes(raw[index:index + 4], "little")
                for index in range(0, len(raw), 4)]


TEST_PROFILE = {
    "type": "AGB",
    "names": ["Test Cart"],
    "flash_ids": [[0x20, 0x00, 0x0E, 0x88]],
    "flash_size": 0x2000000,
    "sector_size_from_cfi": True,
    "command_set": "INTEL",
    "commands": {
        "reset": [[0, 0x50], [0, 0xFF]],
        "read_identifier": [[0, 0x90]],
        "read_cfi": [[0xAA, 0x98]],
        "sector_erase": [["SA", 0x60], ["SA", 0xD0]],
        "sector_erase_wait_for": [[None, None, None],
                                  ["SA", 0x80, 0xFFFF]],
        "buffer_write": [["SA", 0xE8], ["SA", "BS"], ["PA", "PD"],
                         ["SA", 0xD0], ["SA", 0xFF]],
        "buffer_write_wait_for": [[None, None, None], [None, None, None],
                                  [None, None, None],
                                  ["SA", 0x80, 0xFFFF],
                                  [None, None, None]],
    },
}


class DetectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write_profile(self, spec):
        path = Path(self.tmp.name) / "fc_AGB_TEST.txt"
        path.write_text(json.dumps(spec))
        return path

    def test_detects_a_matching_flash_id(self):
        spec = {
            "type": "AGB",
            "names": ["Test Cart"],
            "flash_ids": [[0x20, 0x00, 0x0E, 0x88]],
            "flash_size": 0x2000000,
            "sector_size_from_cfi": True,
            "command_set": "INTEL",
            "commands": TEST_PROFILE["commands"],
        }
        path = self.write_profile(spec)
        profiles = flashgbx.load_profiles(self.tmp.name)
        cfi = build_cfi_buffer(erase_regions=((0x10000, 255), (0x8000, 4)))
        link = FakeLink(b"ROM-ROM-ROM-", b"\x20\x00\x0E\x88", cfi)
        result = flash.detect(link, profiles)
        self.assertIsNotNone(result.profile)
        self.assertEqual(result.profile.primary_name, "Test Cart")
        self.assertEqual(result.flash_id, b"\x20\x00\x0E\x88")
        self.assertEqual(result.cfi["size"], 1 << 25)
        self.assertFalse(result.ambiguous)

    def test_ignores_a_cartridge_that_does_not_enter_id_mode(self):
        spec = dict(TEST_PROFILE, names=["Test Cart"],
                    flash_ids=[[0x20, 0x00, 0x0E, 0x88]])
        self.write_profile(spec)
        profiles = flashgbx.load_profiles(self.tmp.name)
        link = FakeLink(b"\xFF" * 8, b"\xFF" * 8,
                        build_cfi_buffer())
        result = flash.detect(link, profiles)
        self.assertIsNone(result.profile)

    def test_rejects_a_profile_whose_cfi_size_disagrees(self):
        spec = dict(TEST_PROFILE, names=["Wrong Size"],
                    flash_ids=[[0x20, 0x00, 0x0E, 0x88]],
                    flash_size=0x100000)
        self.write_profile(spec)
        profiles = flashgbx.load_profiles(self.tmp.name)
        # A 1 MiB profile cannot match a 32 MiB CFI response; the candidate is
        # still kept because it is the only ID match, but the mismatch is
        # visible in the result.
        cfi = build_cfi_buffer(size_log2=25)
        link = FakeLink(b"ROM-ROM-", b"\x20\x00\x0E\x88", cfi)
        result = flash.detect(link, profiles)
        self.assertIsNotNone(result.profile)
        self.assertNotEqual(result.profile.flash_size, result.cfi["size"])

    def test_full_profile_set_selects_the_m36(self):
        profiles = flashgbx.load_profiles(FIXTURES)
        cfi = build_cfi_buffer(
            erase_regions=((0x20000, 255), (0x8000, 4)))
        link = FakeLink(b"ROM-ROM-ROM-", b"\x20\x00\x0E\x88", cfi)
        result = flash.detect(link, profiles)
        self.assertIsNotNone(result.profile)
        self.assertEqual(result.profile.flash_size, 0x2000000)
        self.assertEqual(result.profile.path.name, "fc_AGB_M36L0R806.txt")
        self.assertEqual(result.flash_id, b"\x20\x00\x0E\x88")
        self.assertFalse(result.ambiguous)
        self.assertEqual(result.cfi["size"], 0x2000000)


if __name__ == "__main__":
    unittest.main()
