"""FlashGBX device backend for the GBA link-port payload.

FlashGBX's architecture assumes a cartridge reader whose *firmware* knows how
to program flash chips: ``LK_Device`` sends small commands such as
``AGB_CART_READ``, ``FLASH_PROGRAM`` and ``SET_FLASH_CMD``, and the firmware on
the other end does the bus work.  This module implements the subset of that
firmware which the GBA payload can service, on top of :mod:`flashgblink.gbalink`.

The payload lives in the GBA's RAM and is uploaded over multiboot, so the
device only appears once the payload is running.  Uploading is intentionally
not part of ``Initialize``: FlashGBX probes ports in worker threads during
device discovery, and the GBA has to be powered on holding START+SELECT first.
Use ``python3 flash.py upload`` for that step.
"""

import os
import time

from FlashGBX.LK_Device import LK_Device
from FlashGBX.Logging import dprint

from .gbalink import (BLOCK_BYTES, Link, ProtocolError, ST_OK, encode_write_plan,
                      progress, read_region, write_block)

SAVE_BASE = 0x0E000000


class GbxDevice(LK_Device):
    DEVICE_NAME = "FlashGBLink"
    DEVICE_MIN_FW = 1
    DEVICE_MAX_FW = 15
    DEVICE_LATEST_FW_TS = {}
    PCB_VERSIONS = {1: "GBA link port"}
    BAUDRATE = 1_250_000
    MAX_BUFFER_READ = BLOCK_BYTES
    MAX_BUFFER_WRITE = BLOCK_BYTES
    USB_IDS = ((0x2E8A, 0x000A),)
    DEVICE_LABEL_LONG = "FlashGBLink (GBA link port)"
    DEVICE_LABEL_SHORT = "FlashGBLink"
    DEVICE_SUPPORT_MESSAGE = (
        "FlashGBLink uses the GBA link port and a Raspberry Pi Pico. The GBA "
        "must be running the flasher payload. Hold START+SELECT while powering "
        "the GBA on and upload it with `python3 flash.py upload`.")

    def __init__(self):
        self.LINK = None
        self.FW = None
        self.FW_VAR = {}
        self.MODE = None
        self.PORT = ""
        self._replies = bytearray()
        self._data_handler = None
        self._set_flash = None
        self._active_cart = None
        self._plan_key = None
        self._plan_chunk = 0
        self._plan_block = 0
        self._rom_read_addr = 0
        self._program_addr = 0
        self._save_read_addr = 0
        self._save_write_addr = 0
        self._command_set = 0
        self._mode_selected = False
        self._write_active = False
        self._write_total = 0
        self._verify_phase = False
        self._last_percent = [-1, -1]

    # -- connection ---------------------------------------------------------

    def Initialize(self, flashcarts, port=None, max_baud=2000000):
        if self.LINK is not None:
            self.Close()
        if self.DEVICE is not None:
            try:
                self.DEVICE.close()
            except Exception:
                pass
            self.DEVICE = None
        self.FW = None
        self.FW_VAR = {}
        self._replies = bytearray()

        ports = [port] if port is not None else self.GetCandidatePorts()
        if not ports:
            return False
        baud = max(9600, min(int(max_baud or self.BAUDRATE), self.BAUDRATE))
        for candidate in ports:
            link = None
            try:
                link = Link(candidate, baud)
                status = link.ping(timeout=0.5)
            except Exception as error:
                dprint("FlashGBLink: no payload on {}: {}".format(candidate, error))
                if link is not None:
                    link.close()
                continue
            if (status & 0xFFFF0000) != ST_OK:
                link.close()
                continue

            self.LINK = link
            self.DEVICE = link.ser
            self.PORT = candidate
            self.BAUDRATE = baud
            self.FW = {
                "pcb_name": self.DEVICE_NAME,
                "pcb_ver": 1,
                "fw_ver": 15,
                "fw_dt": "2026-11-01",
                "cfw_id": "F",
                "unregistered": False,
                "cart_mode_switch": False,
                "cart_presence_switch": False,
            }
            self.FW_VAR = {
                "CART_MODE": 2,
                "TRANSFER_SIZE": 2,
                "BUFFER_SIZE": 0,
                "AGB_READ_METHOD": 0,
            }
            self.MODE = "AGB"
            self._mode_selected = False
            self.LAST_CHECK_ACTIVE = 0
            self.INFO = {"action": None, "last_action": None, "dump_info": {}}
            self._reset_cursors()
            self.UpdateFlashCarts(flashcarts)
            dprint("FlashGBLink: payload on {}".format(candidate))
            return []
        return False

    def LoadFirmwareVersion(self):
        if self.LINK is None:
            return False
        try:
            status = self.LINK.ping(timeout=0.5)
        except Exception:
            return False
        return (status & 0xFFFF0000) == ST_OK

    def CheckActive(self):
        if self.LAST_CHECK_ACTIVE and time.monotonic() < self.LAST_CHECK_ACTIVE + 1:
            return True
        if self.DEVICE is None or self.LINK is None:
            return False
        try:
            status = self.LINK.ping(timeout=0.3)
        except Exception:
            return False
        if (status & 0xFFFF0000) == ST_OK:
            self.LAST_CHECK_ACTIVE = time.monotonic()
            return True
        return False

    def IsConnected(self):
        return self.CheckActive()

    def Close(self, cartPowerOff=False):
        if self.LINK is not None:
            self.LINK.close()
        self.LINK = None
        self.DEVICE = None
        self.MODE = None

    def GetFirmwareVersion(self, more=False):
        return "payload"

    def ChangeBaudRate(self, baudrate):
        return False

    def GetFullName(self):
        if self.FW is None:
            return self.DEVICE_NAME
        return super().GetFullName()

    def GetFullNameExtended(self, more=False):
        if more:
            return "{:s} on {:s}".format(self.GetFullName(), self.GetPort())
        return "{:s} ({:s})".format(self.GetFullName(), self.GetPort())

    def GetSupportedModes(self):
        return ["AGB"]

    def SetTimeout(self, seconds=1):
        # LK_Device would change the serial port timeout; the reader thread
        # and Link.xfer manage their own timeouts, so keep them independent.
        self.DEVICE_TIMEOUT = max(0.1, seconds)

    def GetMode(self):
        # FlashGBX reads the cartridge only after the platform has been
        # selected.  Report no mode until SetMode() has been called.
        return self.MODE if self._mode_selected else None

    def SetMode(self, mode, delay=0.1):
        if mode != "AGB":
            raise ProtocolError("FlashGBLink supports AGB cartridges only")
        self.MODE = "AGB"
        self._mode_selected = True
        self.FW_VAR["CART_MODE"] = 2

    # -- capabilities -------------------------------------------------------

    def CanSetVoltageBySwitch(self):
        return False

    def CanSetVoltageByCode(self):
        return False

    def CanSetVoltageByAutoswitch(self):
        return False

    def CanPowerCycleCart(self):
        # The GBA is the cartridge host; the payload cannot reset it.
        return False

    def IsSupported3dMemory(self):
        return False

    def IsClkConnected(self):
        return False

    def SupportsFirmwareUpdates(self):
        return False

    def FirmwareUpdateAvailable(self):
        return False

    def GetFirmwareUpdaterClass(self):
        return None

    def ResetLEDs(self):
        pass

    def SupportsBootloaderReset(self):
        return False

    def BootloaderReset(self):
        return False

    def SupportsAudioAsWe(self):
        return False

    def CartPowerCycleOrAskReconnect(self):
        self.SetProgress({
            "action": "ABORT",
            "info_type": "msgbox_critical",
            "info_msg": (
                "This operation needs a cartridge power cycle. Power the GBA "
                "off and on while holding START+SELECT, run "
                "`python3 flash.py upload`, then click Connect again."),
            "abortable": False,
        })
        return False

    # -- virtual flashcart firmware ----------------------------------------

    def _reset_cursors(self):
        self._rom_read_addr = 0
        self._program_addr = 0
        self._save_read_addr = 0
        self._save_write_addr = 0

    def _ack(self):
        self._replies.extend(b"\x01")

    def _queue(self, data):
        if isinstance(data, int):
            data = bytes([data])
        self._replies.extend(bytes(data))

    def _read(self, count):
        if count == 1:
            if not self._replies:
                return False
            value = self._replies[0]
            del self._replies[:1]
            return value
        if len(self._replies) < count:
            return False
        data = bytearray(self._replies[:count])
        del self._replies[:count]
        return data

    def _set_fw_variable(self, key, value, log=True):
        self.FW_VAR[key] = value
        if key == "ADDRESS":
            # ROM commands use a 16-bit word address; save commands use a
            # byte address.  Keep both cursors ready; each handler picks its
            # own.
            self._rom_read_addr = int(value) * 2
            self._program_addr = int(value) * 2
            self._save_read_addr = int(value)
            self._save_write_addr = int(value)
        elif key == "BUFFER_SIZE":
            self._plan_key = None
        return True

    def _get_fw_variable(self, key):
        return self.FW_VAR.get(key, 0)

    def GetVarState(self):
        return dict(self.FW_VAR)

    def SetVarState(self, var_state):
        if isinstance(var_state, dict):
            self.FW_VAR.update(var_state)

    def _command_set_name(self):
        if self._command_set == 1:
            return "AMD"
        if self._command_set == 2:
            return "INTEL"
        return ""

    def _store_flash_config_byte(self, value):
        if self._set_flash is None:
            return
        step = self._set_flash["step"]
        if step < 3:
            self._set_flash[("command_set", "method", "we")[step]] = value
            if step == 0:
                self._command_set = value
            self._set_flash["step"] = step + 1
            return
        self._set_flash["row"] = self._set_flash.get("row", b"") + bytes([value])
        if len(self._set_flash["row"]) == 6:
            self._set_flash["rows"].append(self._set_flash["row"])
            self._set_flash["row"] = b""
        if len(self._set_flash["rows"]) == 6:
            dprint("FlashGBLink: flash config {}".format(self._set_flash))
            self._set_flash = None
            self._plan_key = None
            # LK_Device sends SET_FLASH_CMD in pieces and then waits for one
            # acknowledgement, after the last row.
            self._ack()

    def _write(self, data, wait=False):
        if not isinstance(data, bytearray):
            data = bytearray([data])

        if self._set_flash is not None:
            for value in data:
                self._store_flash_config_byte(value)
            if wait:
                return 1
            return

        if self._data_handler is not None:
            handler, self._data_handler = self._data_handler, None
            handler(bytes(data))
            if wait:
                return 1
            return

        command = self.DEVICE_CMD

        if data[0] == command["PING"]:
            if len(data) > 1:
                self._queue((~data[1]) & 0xFF)
            else:
                self._queue(0x01)
            return

        if data[0] == command["SET_FLASH_CMD"]:
            self._set_flash = {
                "step": 0, "command_set": 0, "method": 0, "we": 0,
                "rows": [], "row": b"",
            }
            return

        if data[0] in (command["AGB_CART_READ"],):
            self._read_rom()
            return

        if data[0] == command["AGB_CART_READ_SRAM"]:
            self._read_save()
            return

        if data[0] == command["AGB_CART_READ_EEPROM"]:
            size = int(self.FW_VAR.get("TRANSFER_SIZE") or 0)
            self._queue(b"\xff" * size)
            return

        if data[0] == command["AGB_READ_GPIO_RTC"]:
            # No RTC on the link path; eight zero bytes mean "not present".
            self._queue(b"\x00" * 8)
            return

        if data[0] == command["GET_VARIABLE"]:
            self._queue(b"\x00\x00\x00\x00")
            return

        if data[0] in (command["AGB_CART_WRITE"],
                       command["AGB_FLASH_WRITE_SHORT"]):
            self._write_word(data)
        elif data[0] == command["CART_WRITE_FLASH_CMD"]:
            self._write_flash_commands(data)
        elif data[0] == command["AGB_CART_WRITE_SRAM"]:
            self._data_handler = self._write_save
        elif data[0] == command["AGB_CART_WRITE_EEPROM"]:
            self._data_handler = self._write_eeprom
        elif data[0] == command["AGB_CART_WRITE_FLASH_DATA"]:
            self._data_handler = self._write_save_flash
        elif data[0] == command["FLASH_PROGRAM"]:
            self._data_handler = self._program
        # SET_VARIABLE is intercepted by _set_fw_variable.  Mode, voltage,
        # pull-up, LED, boot and pin commands are deliberately no-ops: the
        # GBA owns the cartridge bus.

        if wait:
            return 1

    # -- raw bus operations -------------------------------------------------

    def _read_rom(self):
        size = int(self.FW_VAR.get("TRANSFER_SIZE") or 0)
        if size <= 0:
            self._queue(b"")
            return
        data = read_region(self.LINK, self._rom_read_addr, size)
        self._rom_read_addr += size
        self._queue(data)

    def _read_save(self):
        size = int(self.FW_VAR.get("TRANSFER_SIZE") or 0)
        data = bytearray()
        offset = 0
        while offset < size:
            chunk = min(BLOCK_BYTES, size - offset)
            data += self.LINK.read8(SAVE_BASE + self._save_read_addr + offset,
                                    chunk)
            offset += chunk
        self._save_read_addr += size
        self._queue(bytes(data))

    def _write_word(self, data):
        if len(data) < 7:
            raise ProtocolError("short AGB write command")
        word_address = int.from_bytes(data[1:5], "big")
        value = int.from_bytes(data[5:7], "big")
        self.LINK.write_seq([(word_address * 2, value)])

    def _write_flash_commands(self, data):
        if len(data) < 3:
            raise ProtocolError("short flash command frame")
        rom_space = bool(data[1])
        count = data[2]
        offset = 3
        pairs = []
        save_writes = []
        for _ in range(count):
            if len(data) < offset + 6:
                raise ProtocolError("short flash command table")
            address = int.from_bytes(data[offset:offset + 4], "big")
            value = int.from_bytes(data[offset + 4:offset + 6], "big")
            if rom_space:
                # AGB ROM commands carry a 16-bit word address.
                pairs.append((address * 2, value))
            else:
                # AGB save-flash commands carry a byte address in save space.
                save_writes.append((SAVE_BASE + address, value & 0xFF))
            offset += 6
        if pairs:
            self.LINK.write_seq(pairs)
        for address, value in save_writes:
            self.LINK.write8(address, bytes([value]))
        self._ack()

    def _write_save(self, data):
        offset = 0
        while offset < len(data):
            chunk = data[offset:offset + BLOCK_BYTES]
            self.LINK.write8(SAVE_BASE + self._save_write_addr + offset, chunk)
            offset += len(chunk)
        self._save_write_addr += len(data)

    def _write_eeprom(self, data):
        raise ProtocolError(
            "FlashGBLink does not support EEPROM saves yet; the payload has "
            "no EEPROM bit-banging path")

    def _write_save_flash(self, data):
        raise ProtocolError(
            "FlashGBLink does not support save flash writing yet; only SRAM "
            "save dump and restore are implemented")

    # -- flash programming --------------------------------------------------

    def _FlashROM_Worker(self, args):
        self._active_cart = None
        index = args.get("cart_type")
        try:
            if index:
                carts = self.GetSupportedCartridgesAGB()[1]
                if 0 <= index < len(carts) and isinstance(carts[index], dict):
                    self._active_cart = carts[index]
        except Exception:
            self._active_cart = None
        self._plan_key = None
        self._write_total = 0
        try:
            self._write_total = (len(args["buffer"]) if "buffer" in args
                                 else os.path.getsize(args["path"]))
        except (KeyError, OSError, TypeError):
            self._write_total = 0
        self._write_active = True
        self._verify_phase = False
        self._last_percent = [-1, -1]
        self._update_progress(0)
        try:
            return super()._FlashROM_Worker(args)
        finally:
            self._write_active = False
            self._write_total = 0
            self._active_cart = None
            self._plan_key = None

    def SetProgress(self, args, signal=None):
        result = super().SetProgress(args, signal)
        if not self._write_active or self.LINK is None:
            return result
        action = args.get("action")
        if action == "READ":
            # FlashGBX verifies the image with a second read pass; switch to
            # the cyan bar and leave the green one full.
            if not self._verify_phase:
                self._update_progress(self._write_total)
                self._verify_phase = True
                self._last_percent[1] = -1
                self._update_progress(0)
        elif action == "FINISHED":
            self._update_progress(self._write_total)
        elif action in ("UPDATE_POS", "SECTOR_ERASE", "WRITE"):
            pos = args.get("pos")
            if pos is None:
                pos = args.get("sector_pos")
            if pos is not None:
                self._update_progress(pos)
        return result

    def _update_progress(self, position):
        if not self._write_active or not self._write_total or self.LINK is None:
            return
        phase = 1 if self._verify_phase else 0
        position = max(0, min(self._write_total, position))
        percent = int(100 * position // self._write_total)
        if percent == self._last_percent[phase]:
            return
        # The bar is coarse; two-percent steps keep the link overhead small.
        if (percent not in (0, 100)
                and percent < self._last_percent[phase] + 2):
            return
        self._last_percent[phase] = percent
        try:
            progress(self.LINK, percent, phase=phase, timeout=0.3)
        except Exception:
            pass

    def _install_plan(self):
        if self._active_cart is None:
            raise ProtocolError(
                "FlashGBLink needs the selected flashcart profile to program")
        commands = self._active_cart.get("commands") or {}
        name = (self._active_cart.get("names") or ["profile"])[0]
        buffer_size = int(self.FW_VAR.get("BUFFER_SIZE") or 0)
        key = (id(commands), buffer_size, self._command_set)
        if key == self._plan_key:
            return
        chunk, words = encode_write_plan(
            commands, buffer_size,
            command_set=self._command_set_name(), name=name)
        self.LINK.set_write_plan(chunk, words)
        self._plan_chunk = chunk
        self._plan_block = max((BLOCK_BYTES // chunk) * chunk, chunk)
        self._plan_key = key
        dprint("FlashGBLink: write plan for {} ({} bytes, {} commands)"
               .format(name, chunk, len(words) // 4))

    def WriteROM(self, address, buffer, flash_buffer_size=False,
                 skip_init=False, rumble_stop=False, max_length=0x400):
        """Program a ROM buffer at an explicit address.

        FlashGBX's own WriteROM relies on the device firmware advancing its
        address counter across FLASH_PROGRAM frames.  With CompareSectors
        enabled it skips all-0xFF chunks, so a host-side counter drifts.  Send
        every chunk at its known address instead.
        """
        length = min(len(buffer), min(max_length, self.MAX_BUFFER_WRITE))
        if length == 0:
            return False
        if flash_buffer_size:
            self._set_fw_variable("BUFFER_SIZE", flash_buffer_size)
        self._install_plan()
        skipped = False
        for offset in range(0, len(buffer), length):
            chunk = bytes(buffer[offset:offset + length])
            if chunk and chunk == b"\xFF" * len(chunk):
                skipped = True
                continue
            skipped = False
            self._program_at(address + offset, chunk)
            if (rumble_stop and flash_buffer_size
                    and (offset + length) % flash_buffer_size == 0):
                self._cart_write(address=0xC4, value=0x00, flashcart=True)
                self._cart_write(address=0xC6, value=0x00, flashcart=True)
                rumble_stop = False
        self.SKIPPING = skipped
        return None

    def _program_at(self, address, data):
        if not data:
            return
        self._install_plan()
        unit = self._plan_chunk if self._plan_chunk % 4 == 0 else 4
        padding = (-len(data)) % unit
        payload = data + b"\xff" * padding
        for offset in range(0, len(payload), self._plan_block):
            block = payload[offset:offset + self._plan_block]
            write_block(self.LINK, address + offset, block)

    def _program(self, data):
        if not data:
            return
        self._install_plan()
        unit = self._plan_chunk if self._plan_chunk % 4 == 0 else 4
        padding = (-len(data)) % unit
        payload = data + b"\xff" * padding
        address = self._program_addr
        for offset in range(0, len(payload), self._plan_block):
            block = payload[offset:offset + self._plan_block]
            write_block(self.LINK, address + offset, block)
        # The firmware advances by the logical length, not the padding.
        self._program_addr += len(data)
