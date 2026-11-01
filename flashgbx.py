"""FlashGBX profile files and CFI decoding.

FlashGBX ships one JSON file per cartridge type in its configuration
directory.  Those files use C-style hexadecimal literals, so the constants are
converted before they are handed to :mod:`json`.  Profiles are read from the
FlashGBX installation; this project does not carry a second copy.  Cartridge
detection and CFI decoding are delegated to FlashGBX's ``Flashcart`` and
``CFI`` classes.

The profile format and the files come from FlashGBX
(https://github.com/Lesserkuma/FlashGBX), GPL-3.0-or-later.
"""

import json
import os
import re
import sys
from pathlib import Path

from gbalink.errors import ProtocolError

_HEX = re.compile(r"0x[0-9A-Fa-f]+")


def _config_candidates():
    env = os.environ.get("FLASHGBLINK_CONFIG")
    if env:
        yield Path(env).expanduser()
    if sys.platform == "darwin":
        yield Path("/Applications/FlashGBX.app/Contents/MacOS/config")
        yield Path.home() / "Library/Preferences/FlashGBX"
        yield Path.home() / "Library/Application Support/FlashGBX"
    elif os.name == "nt":
        for variable in ("LOCALAPPDATA", "APPDATA"):
            base = os.environ.get(variable)
            if base:
                yield Path(base) / "FlashGBX"
    else:
        yield Path.home() / ".config/FlashGBX"
        yield Path.home() / ".config/flashgbx"


def find_config_dir(directory=None):
    """Locate the FlashGBX profile directory, or an explicit override."""
    if directory:
        path = Path(directory).expanduser()
        if not path.is_dir():
            raise ProfileError("profile directory not found: %s" % path)
        return path
    for candidate in _config_candidates():
        if candidate.is_dir() and any(candidate.glob("fc_AGB_*.txt")):
            return candidate
    raise ProfileError(
        "no FlashGBX profile directory found; install FlashGBX, run it once, "
        "or pass --config / set FLASHGBLINK_CONFIG")

# Command sets whose identification this payload/bridge cannot perform.  Their
# sequences need board-specific handling (bank switching, encrypted access or a
# different bus), so they stay selectable by name but are not autodetected.
NO_AUTODETECT = frozenset({
    "3DMEMORY",
    "GBAMP",
    "SINTAX_YJENCRYPTED",
    "VASTFAME",
})

# These command sets cannot write a ROM image through the generic 16-bit
# template engine, even though their identification commands work.
UNSUPPORTED_WRITE = NO_AUTODETECT | frozenset({"EEPROM"})


class ProfileError(ProtocolError):
    pass


class CfiError(ProtocolError):
    pass


def swap_bits(value, pair):
    """Swap two data bits in a byte, as some cartridge PCBs wire them."""
    low, high = pair
    if ((value >> low) & 1) ^ ((value >> high) & 1):
        value ^= (1 << low) | (1 << high)
    return value


def parse_cfi(buffer):
    """Decode a raw CFI query response with FlashGBX's parser.

    The returned ``regions`` are ``(block_bytes, count)`` pairs in the order
    the chip reports them.  ``boot_sector_raw`` is 0x03 when the chip's sector
    table is stored in reverse order; use :func:`cfi_sector_regions` instead of
    reading ``regions`` directly when building an offset map.
    """
    if len(buffer) < 0x200:
        raise CfiError("CFI read returned %d bytes, expected at least 512"
                       % len(buffer))
    try:
        from FlashGBX.Flashcart import CFI
    except ImportError as error:  # pragma: no cover - environment dependent
        raise CfiError("FlashGBX is required for CFI decoding: %s" % error)
    info = CFI().Parse(bytearray(buffer))
    if info is False:
        magic = bytes(buffer[0x20:0x21] + buffer[0x22:0x23]
                      + buffer[0x24:0x25])
        raise CfiError("no CFI signature at 0x20/0x22/0x24 (%r)" % magic)
    return normalize_cfi(info, buffer)


def normalize_cfi(info, raw=None):
    """Convert FlashGBX's CFI dictionary to the shape this tool uses."""
    pins = info.get("d_swap")
    if not pins or pins == [(0, 0)]:
        pins = []
    regions = [(int(block[0]), int(block[1]))
               for block in info.get("erase_sector_blocks", [])]
    return {
        "pins": pins,
        "size": int(info["device_size"]),
        "vdd_min": info.get("vdd_min"),
        "vdd_max": info.get("vdd_max"),
        "regions": regions,
        "boot_sector_raw": int(info.get("tb_boot_sector_raw") or 0),
        "buffer_size": info.get("buffer_size"),
        "data": bytes(raw) if raw is not None else info.get("raw", b""),
    }


def cfi_sector_regions(cfi):
    """CFI erase regions in address order: ``[(block_bytes, count), ...]``."""
    regions = list(cfi.get("regions", []))
    if cfi.get("boot_sector_raw") == 0x03:
        regions.reverse()
    return regions


def _expand_regions(regions, limit=0):
    """Turn ``(block_bytes, count)`` pairs into ``(offset, size)`` sectors."""
    sectors = []
    position = 0
    for block, count in regions:
        for _ in range(count):
            if limit and position >= limit:
                return sectors
            sectors.append((position, block))
            position += block
    return sectors


def _hex_to_int(match):
    return str(int(match.group(0), 16))


class Profile:
    """One FlashGBX ``fc_AGB_*.txt`` file.

    All names in the file share the same commands, so any of them can select
    the profile.  ``primary_name`` is the first one, matching how FlashGBX
    labels an autodetected cartridge.
    """

    def __init__(self, spec, path=None):
        if not isinstance(spec, dict):
            raise ProfileError("profile is not a JSON object")
        if str(spec.get("type", "")).upper() != "AGB":
            raise ProfileError("profile type is %r, expected AGB"
                               % spec.get("type"))
        if not spec.get("names"):
            raise ProfileError("profile has no names")
        self.spec = spec
        self.path = Path(path) if path is not None else None

    @property
    def names(self):
        return list(self.spec["names"])

    @property
    def primary_name(self):
        return self.spec["names"][0]

    @property
    def command_set(self):
        return str(self.spec.get("command_set", "")).upper()

    @property
    def flash_size(self):
        return self.spec.get("flash_size")

    @property
    def reset_every(self):
        return self.spec.get("reset_every")

    @property
    def read_identifier_at(self):
        return self.spec.get("read_identifier_at", 0)

    @property
    def flash_id_offsets(self):
        return list(self.spec.get("flash_id_offsets", []))

    @property
    def flash_ids(self):
        ids = []
        for entry in self.spec.get("flash_ids", []):
            if entry and all(isinstance(value, int) for value in entry):
                ids.append(list(entry))
        return ids

    @property
    def commands(self):
        return self.spec.get("commands", {})

    def command(self, key):
        return [list(row) for row in self.commands.get(key, [])]

    def has_command(self, key):
        return bool(self.commands.get(key))

    def matches_name(self, name):
        folded = name.casefold()
        return any(folded == candidate.casefold() for candidate in self.names)

    def is_autodetectable(self):
        return (not self.spec.get("manual_select")
                and self.command_set not in NO_AUTODETECT
                and bool(self.command("read_identifier"))
                and bool(self.flash_ids))

    def supports_write(self):
        return (self.command_set not in UNSUPPORTED_WRITE
                and (self.has_command("buffer_write")
                     or self.has_command("single_write")))

    def select_id_bytes(self, buffer):
        """Pick the identification bytes described by ``flash_id_offsets``."""
        if not self.flash_id_offsets:
            return bytes(buffer)
        data = bytes(buffer)
        selected = []
        for offset in self.flash_id_offsets:
            if offset >= len(data):
                return b""
            selected.append(data[offset])
        return bytes(selected)

    def cfi_regions(self, cfi):
        """Sector regions for this profile, using ``sector_size`` or the CFI."""
        value = self.spec.get("sector_size")
        if isinstance(value, bool):
            value = None
        if isinstance(value, int):
            size = self.flash_size or (cfi["size"] if cfi else 0)
            if size <= 0:
                return None
            count = (size + value - 1) // value
            return [(value, count)]
        if isinstance(value, list):
            regions = []
            for row in value:
                if not isinstance(row, list) or len(row) < 2:
                    return None
                regions.append((int(row[0]), int(row[1])))
            return regions
        if cfi:
            regions = cfi_sector_regions(cfi)
            if regions:
                return regions
        return None

    def total_size(self, cfi=None):
        if self.flash_size:
            return self.flash_size
        if cfi:
            return cfi["size"]
        return None

    def sector_map(self, length, cfi=None):
        """Sectors as ``(offset, size)`` up to at least ``length`` bytes.

        Returns None when the profile has no sector information and no CFI
        data was supplied.  The returned list may extend past ``length`` for a
        uniform sector size, which lets the caller pad an image to a sector
        boundary.
        """
        regions = self.cfi_regions(cfi)
        if not regions:
            return None
        sectors = _expand_regions(regions)
        if not sectors:
            return None
        if length <= 0:
            return sectors
        # A uniform sector size is expanded from flash_size, which may be
        # larger than the image; that is fine.  Otherwise stop at the last
        # sector that touches the image.
        end = None
        for index, (offset, size) in enumerate(sectors):
            if offset < length <= offset + size:
                end = index + 1
                break
        if end is not None:
            return sectors[:end]
        return sectors

    def pad_to_sector(self, length, cfi=None):
        """Length rounded up to the end of the sector containing it."""
        sectors = self.sector_map(length, cfi)
        if not sectors:
            return length
        for offset, size in sectors:
            if offset < length <= offset + size:
                return offset + size
        if sectors and length > sectors[-1][0] + sectors[-1][1]:
            return length
        return sectors[0][0] + sectors[0][1]

    def buffer_size(self, cfi=None):
        """Buffered-write window in bytes, from the profile or the CFI."""
        value = self.spec.get("buffer_size")
        if isinstance(value, int) and value > 0:
            return value
        if self.has_command("buffer_write") and cfi:
            value = cfi.get("buffer_size")
            if isinstance(value, int) and value > 0:
                return value
        return None


def parse_profile_text(text, path=None):
    try:
        spec = json.loads(_HEX.sub(_hex_to_int, text))
    except (TypeError, ValueError) as error:
        raise ProfileError("%s: %s" % (path or "profile", error))
    return Profile(spec, path)


def load_profile(path):
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ProfileError("%s: %s" % (path, error))
    return parse_profile_text(text, path)


def load_profiles(directory=None):
    """Load every ``fc_AGB_*.txt`` profile in ``directory``, file order."""
    directory = find_config_dir(directory)
    profiles = []
    for path in sorted(directory.glob("fc_AGB_*.txt")):
        profiles.append(load_profile(path))
    if not profiles:
        raise ProfileError("no fc_AGB_*.txt profiles in %s" % directory)
    return profiles


def find_profile(profiles, name):
    for profile in profiles:
        if profile.matches_name(name):
            return profile
    return None


def relocate_commands(commands, address):
    """Move a command sequence to ``address`` like FlashGBX does.

    Only rows that use the sequence's original first address are moved; rows
    that write an unlock address (0xAAA, 0x555) stay where they are.
    """
    if not commands:
        return []
    old_address = commands[0][0]
    return [[address if row[0] == old_address else row[0]] + list(row[1:])
            for row in commands]
