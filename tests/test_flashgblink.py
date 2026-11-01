"""FlashGBLink adapter tests that do not need hardware."""

import json
import re
import unittest
from pathlib import Path

try:
    import FlashGBX.app  # noqa: F401
    from FlashGBX.LK_Device import LK_Device  # noqa: F401
    HAVE_FLASHGBX = True
except ImportError:  # pragma: no cover - environment dependent
    HAVE_FLASHGBX = False

ROOT = Path(__file__).resolve().parent.parent
M36 = ROOT / "tests" / "fixtures" / "fc_AGB_M36L0R806.txt"


def load_profile(path):
    text = path.read_text(encoding="utf-8")
    return json.loads(re.sub(r"0x[0-9A-Fa-f]+",
                             lambda match: str(int(match.group(0), 16)), text))


@unittest.skipUnless(HAVE_FLASHGBX, "FlashGBX is not installed")
class AdapterTests(unittest.TestCase):
    def setUp(self):
        import flashgblink.device as device_module
        from flashgblink.device import GbxDevice

        self.device_module = device_module
        self.device = GbxDevice()
        self.device.LINK = FakeLink()
        self.device.FW = {"fw_ver": 15, "pcb_name": "FlashGBLink",
                          "pcb_ver": 1, "fw_dt": "", "cfw_id": "F",
                          "unregistered": False, "cart_mode_switch": False,
                          "cart_presence_switch": False}
        self.device.FW_VAR = {"CART_MODE": 2, "TRANSFER_SIZE": 2,
                              "BUFFER_SIZE": 64}
        self.device.MODE = "AGB"
        self.device.INFO = {"action": None, "last_action": None,
                            "dump_info": {}}
        self.profile = load_profile(M36)
        self.device._active_cart = self.profile

        self.original_write_block = device_module.write_block
        device_module.write_block = lambda link, address, data: link.blocks.append(
            (address, bytes(data)))

    def tearDown(self):
        self.device_module.write_block = self.original_write_block



    def test_flashcart_detects_the_chip_through_the_adapter(self):
        try:
            from FlashGBX.Flashcart import Flashcart
        except ImportError:  # pragma: no cover
            self.skipTest("FlashGBX Flashcart is not available")

        self.device.LINK = ChipLink()
        dev = self.device
        flashcart = Flashcart(config=self.profile, fncptr={
            "cart_write_fncptr": dev._cart_write,
            "cart_write_fast_fncptr": dev._cart_write_flash,
            "cart_read_fncptr": dev.ReadROM,
            "cart_powercycle_fncptr": lambda: None,
            "progress_fncptr": dev.SetProgress,
            "set_we_pin_wr": dev._set_we_pin_wr,
            "set_we_pin_audio": dev._set_we_pin_audio,
        })
        verified, flash_id = flashcart.VerifyFlashID()
        self.assertTrue(verified)
        self.assertEqual(flash_id[:4], [0x20, 0x00, 0x0E, 0x88])
        cfi = flashcart.ReadCFI()
        self.assertIsNot(cfi, False)
        self.assertEqual(cfi["device_size"], 0x2000000)

    def test_initialize_activates_a_running_payload(self):
        from gbalink import ST_OK

        class FakeSerial:
            def close(self):
                pass

        class InitLink(FakeLink):
            def __init__(self, port, baud):
                super().__init__()
                self.ser = FakeSerial()

            def ping(self, timeout=5.0):
                return ST_OK

            def close(self):
                pass

        original = self.device_module.Link
        self.device_module.Link = InitLink
        try:
            self.device.GetCandidatePorts = lambda: ["/dev/fake"]
            ret = self.device.Initialize(
                {"DMG": {}, "AGB": {"X": {"names": ["X"], "type": "AGB"}}})
        finally:
            self.device_module.Link = original
        self.assertEqual(ret, [])
        self.assertEqual(self.device.MODE, "AGB")
        self.assertEqual(self.device.PORT, "/dev/fake")
        self.assertIn("X", self.device.GetSupportedCartridgesAGB()[0])

    def test_plan_matches_the_m36_profile(self):
        from gbalink import ADDR_PA, ADDR_SA, VAL_BS, VAL_PD

        chunk, words = self.device_module.encode_write_plan(
            self.profile["commands"], 64, command_set="INTEL",
            name="M36")
        self.assertEqual(chunk, 64)
        self.assertEqual(len(words) // 4, 5)
        self.assertEqual(words[0] >> 30, ADDR_SA)
        self.assertEqual(words[5] >> 30, VAL_BS)
        self.assertEqual(words[8] >> 30, ADDR_PA)
        self.assertEqual(words[9] >> 30, VAL_PD)




    def test_write_and_verify_use_separate_bars(self):
        calls = []
        original = self.device_module.progress
        self.device_module.progress = (
            lambda link, percent, phase=0, timeout=2.0:
            calls.append((percent, phase)))
        try:
            self.device._write_active = True
            self.device._write_total = 1000
            self.device._verify_phase = False
            self.device._last_percent = [-1, -1]
            self.device.SetProgress({"action": "UPDATE_POS", "pos": 1000})
            self.device.SetProgress({"action": "READ", "bytes_added": 4096})
            self.device.SetProgress({"action": "UPDATE_POS", "pos": 500})
            self.device.SetProgress({"action": "FINISHED"})
        finally:
            self.device_module.progress = original
        self.assertEqual(calls, [(100, 0), (0, 1), (50, 1), (100, 1)])


    def test_write_rom_skips_ff_without_drifting_the_address(self):
        writes = []
        original = self.device_module.write_block
        self.device_module.write_block = (
            lambda link, address, data: writes.append((address, bytes(data))))
        try:
            self.device._active_cart = self.profile
            self.device.FW_VAR["BUFFER_SIZE"] = 64
            payload = b"\xFF" * 4096 + bytes(range(256)) * 16
            self.device.WriteROM(0, payload, flash_buffer_size=64,
                                 max_length=4096)
        finally:
            self.device_module.write_block = original
        self.assertEqual(writes[0][0], 4096)
        self.assertEqual(writes[0][1], payload[4096:8192])

    def test_rom_write_progress_is_sent_to_the_payload(self):
        calls = []
        original = self.device_module.progress
        self.device_module.progress = (
            lambda link, percent, phase=0, timeout=2.0:
            calls.append((percent, phase)))
        try:
            self.device._write_active = True
            self.device._write_total = 1000
            self.device._verify_phase = False
            self.device._last_percent = [-1, -1]
            self.device.SetProgress({"action": "UPDATE_POS", "pos": 0})
            self.device.SetProgress({"action": "UPDATE_POS", "pos": 10})
            self.device.SetProgress({"action": "UPDATE_POS", "pos": 250})
            self.device.SetProgress({"action": "FINISHED"})
        finally:
            self.device_module.progress = original
        self.assertEqual(calls, [(0, 0), (25, 0), (100, 0)])

    def test_wait_ack_is_returned_not_queued(self):
        self.assertEqual(
            self.device._write(self.device.DEVICE_CMD["SET_MODE_AGB"], wait=True),
            1)
        self.assertEqual(len(self.device._replies), 0)

    def test_set_flash_cmd_is_reassembled_and_acked(self):
        command = self.device.DEVICE_CMD
        self.device._write(bytearray([command["SET_FLASH_CMD"]]))
        self.device._write(bytearray([2]))   # Intel
        self.device._write(bytearray([2]))   # buffered
        self.device._write(bytearray([0]))   # WE pin
        rows = list(self.profile["commands"]["buffer_write"])
        rows += [[0, 0]] * (6 - len(rows))   # LK_Device always sends six rows
        for row in rows:
            address = 0 if not isinstance(row[0], int) else row[0]
            value = 0 if not isinstance(row[1], int) else row[1]
            self.device._write(bytearray(address.to_bytes(4, "big")
                                         + value.to_bytes(2, "big")))
        self.assertIsNone(self.device._set_flash)
        self.assertEqual(self.device._command_set, 2)
        self.assertEqual(self.device.wait_for_ack(), 1)

    def test_rom_read_advances_the_address(self):
        command = self.device.DEVICE_CMD
        self.device._set_fw_variable("ADDRESS", 0)
        self.device._set_fw_variable("TRANSFER_SIZE", 8)
        self.device._write(bytearray([command["AGB_CART_READ"]]))
        self.assertEqual(self.device._read(8), bytearray(b"01234567"))
        self.device._write(bytearray([command["AGB_CART_READ"]]))
        self.assertEqual(self.device._read(8), bytearray(b"89ABCDEF"))

    def test_save_write_uses_the_byte_bus(self):
        command = self.device.DEVICE_CMD
        self.device._set_fw_variable("ADDRESS", 0x10)
        self.device._set_fw_variable("TRANSFER_SIZE", 2)
        self.device._write(bytearray([command["AGB_CART_WRITE_SRAM"]]))
        self.assertEqual(self.device._write(bytearray(b"\xAA\xBB"), wait=True), 1)
        self.assertEqual(self.device.LINK.saved[0x10:0x12], b"\xAA\xBB")


    def test_flash_commands_route_rom_and_save_space(self):
        command = self.device.DEVICE_CMD
        rom = bytearray([command["CART_WRITE_FLASH_CMD"], 1, 2]) + bytearray(
            (0x2AAA).to_bytes(4, "big") + (0x90).to_bytes(2, "big")) + bytearray(
            (0x2AAA).to_bytes(4, "big") + (0x90).to_bytes(2, "big"))
        self.device._write(rom)
        self.assertEqual(self.device._read(1), 1)
        self.assertEqual(self.device.LINK.seq,
                         [(0x5554, 0x90), (0x5554, 0x90)])

        save = bytearray([command["CART_WRITE_FLASH_CMD"], 0, 1]) + bytearray(
            (0x5555).to_bytes(4, "big") + (0xAA).to_bytes(2, "big"))
        self.device._write(save)
        self.assertEqual(self.device._read(1), 1)
        self.assertEqual(self.device.LINK.saved[0x5555], 0xAA)

    def test_flash_program_streams_a_plan(self):
        command = self.device.DEVICE_CMD
        self.device._set_fw_variable("ADDRESS", 0)
        self.device._set_fw_variable("TRANSFER_SIZE", 64)
        self.device._write(bytearray([command["FLASH_PROGRAM"]]))
        self.assertEqual(self.device._write(bytearray(range(64)), wait=True), 1)
        self.assertEqual(self.device.LINK.plans, [(64, 5)])
        self.assertEqual(self.device.LINK.blocks,
                         [(0, bytes(range(64)))])
        # The firmware advances ADDRESS itself between FLASH_PROGRAM frames.
        self.device._write(bytearray([command["FLASH_PROGRAM"]]))
        self.device._write(bytearray([0xFF] * 64), wait=True)
        self.assertEqual(self.device.LINK.blocks[-1][0], 64)

    def test_program_pads_a_partial_frame(self):
        command = self.device.DEVICE_CMD
        self.device._set_fw_variable("ADDRESS", 0)
        self.device._set_fw_variable("TRANSFER_SIZE", 4)
        self.device._write(bytearray([command["FLASH_PROGRAM"]]))
        self.device._write(bytearray(b"\x01\x02\x03\x04"), wait=True)
        self.assertEqual(self.device.LINK.blocks,
                         [(0, b"\x01\x02\x03\x04" + b"\xFF" * 60)])


@unittest.skipUnless(HAVE_FLASHGBX, "FlashGBX is not installed")
class RegistrationTests(unittest.TestCase):
    def test_install_appends_the_backend_once(self):
        import FlashGBX.app as app
        import flashgblink.device as device_module
        from flashgblink import install

        install()
        install()
        self.assertEqual(app.HW_DEVICES.count(device_module), 1)


class FakeLink:
    def __init__(self):
        self.rom = bytearray(b"0123456789ABCDEF" * 8)
        self.saved = bytearray(0x10000)
        self.plans = []
        self.blocks = []
        self.seq = []

    def read_words(self, address, count):
        data = bytes(self.rom[address:address + count * 4])
        data = data.ljust(count * 4, b"\xFF")
        return [int.from_bytes(data[index:index + 4], "little")
                for index in range(0, len(data), 4)]

    def read8(self, address, count):
        if address >= 0x0E000000:
            start = address - 0x0E000000
            return bytes(self.saved[start:start + count])
        return bytes(self.rom[address:address + count])

    def write8(self, address, data):
        start = address - 0x0E000000
        self.saved[start:start + len(data)] = data

    def write_seq(self, pairs):
        self.seq.extend(pairs)

    def set_write_plan(self, chunk, words):
        self.plans.append((chunk, len(words) // 4))

    def close(self):
        pass


class ChipLink(FakeLink):
    """A tiny M36 flash model: enough for ID and CFI reads."""

    ID = b"\x20\x00\x0E\x88\x00\x00\x00\x00"

    def __init__(self):
        super().__init__()
        from tests.test_protocol import build_cfi_buffer

        self.mode = "array"
        self.rom = bytearray(0x1000)
        self.cfi = (bytes(build_cfi_buffer()) + b"\x00" * 0x200)[:0x400]
        self.rom_writes = []

    def write_seq(self, pairs):
        self.rom_writes.extend(pairs)
        for _address, value in pairs:
            if value == 0x90:
                self.mode = "id"
            elif value == 0x98:
                self.mode = "cfi"
            elif value in (0x50, 0xF0, 0xFF):
                self.mode = "array"

    def read_words(self, address, count):
        if self.mode == "id":
            data = self.ID[address:address + count * 4]
        elif self.mode == "cfi":
            data = self.cfi[address:address + count * 4]
        else:
            data = bytes(self.rom[address:address + count * 4])
        data = data.ljust(count * 4, b"\xFF")
        return [int.from_bytes(data[index:index + 4], "little")
                for index in range(0, len(data), 4)]


if __name__ == "__main__":
    unittest.main()
