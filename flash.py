#!/usr/bin/env python3
"""Flash a GBA cartridge through the link port.

Uploads ``payload/flasher.gba`` into the GBA with gbasend, then talks to it over
the same gbasend-pico USB serial bridge.  The GBA payload performs 16-bit
cartridge stores/loads and a soft reset; the host selects a FlashGBX profile and
sends its command template, so the payload does not need to be rebuilt for a new
cartridge.

Quick start (cart inserted, GBA off):

    python3 flash.py upload      # hold START+SELECT, power on
    python3 flash.py detect
    python3 flash.py id
    python3 flash.py cfi

Requires pyserial and the ``gbasend`` CLI from
https://github.com/nullstalgia/gbasend-rs.
"""

import argparse
import copy
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import zlib
from pathlib import Path

from flashgbx import (Profile, ProfileError, ProtocolError,
                      cfi_sector_regions, find_config_dir, find_profile,
                      load_profiles, normalize_cfi, parse_cfi,
                      relocate_commands, swap_bits)
from flashgblink.gbalink import (ADDR_ABS, ADDR_PA, ADDR_SA, BLOCK_BYTES,
                     CAP_READ_CHECK, CAPS_VERSION, HDR_MAGIC,
                     OP_ABORT, OP_BOOT, OP_CAPS, OP_CRC32, OP_CRC_GET, OP_DONE, OP_PROGRESS, OP_READ, OP_READ8,
                     OP_READ8_CHECK, OP_READ_CHECK,
                     OP_SET_PLAN, OP_WRITE8, OP_WRITE_BLOCK, OP_WRITE_SEQ, PAYLOAD,
                     PING, PLAN_MAX_COMMANDS, RESYNC_WORDS, ST_BAD, ST_ECHO,
                     ST_OK, ST_TIMEOUT,
                     VAL_BS, VAL_CONST, VAL_PD, WAIT_ABS, WAIT_NONE, WAIT_PD,
                     WAIT_SA, Link, PlanError, TransferError, UnsupportedCommand,
                     _check_status, _hex, _pack_words, done, find_port, progress,
                     read_bytes, read_region, single_write_waits, write_block)
from flashgblink.gbalink.plan import encode_write_plan as plan_encode_write_plan
from flashgblink.gbalink.plan import prepare_write as plan_prepare_write

# -- M36L0R8060B / Intel command set, from FlashGBX fc_AGB_M36L0R806.txt --
# Kept as the fallback for `id` and `cfi` when no profile is loaded.
ID_SEQ = [(0, 0x90)]
CFI_SEQ = [(0xAA, 0x98)]

STATE_VERSION = 3
STATE_DIR_ENV = "FLASHGBLINK_STATE_DIR"
PROBE_BYTES = 16
CFI_BYTES = 0x400


# -- cartridge operations -----------------------------------------------------

def _read_bytes(link, address, length):
    return read_region(link, address, length)


def _reset_pairs(profile, address):
    if profile is None:
        return [(address, 0x50), (address, 0xFF)]
    if profile.spec.get("synthetic"):
        # Burn2Slot's sequences already carry the exact unlock addresses;
        # moving them to the sector address would change the sequence.
        return [(row[0], row[1]) for row in profile.command("reset")]
    rows = relocate_commands(profile.command("reset"), address)
    return [(row[0], row[1]) for row in rows]


def cart_reset(link, profile=None, address=None, max_address=None):
    """Return the flash chip to read-array mode.

    With ``address`` the reset goes into that bank only; with ``reset_every``
    the chip is reset at every bank boundary, which is what Intel parts with
    per-bank command state need after a power-up or a partial operation.
    """
    if address is not None:
        targets = [address]
    elif profile is not None and profile.reset_every:
        limit = profile.flash_size
        if max_address is not None:
            limit = min(limit, max_address) if limit else max_address
        targets = list(range(0, limit or 0, profile.reset_every)) or [0]
    else:
        targets = [0]
    for target in targets:
        pairs = _reset_pairs(profile, target)
        if pairs:
            link.write_seq(pairs)


def cart_id(link, profile=None):
    address = profile.read_identifier_at if profile else 0
    sequences = []
    if profile is not None:
        sequences.append(profile.command("read_identifier"))
    else:
        sequences.extend([ID_SEQ, [(0xAAA, 0xA9), (0x555, 0x56), (0xAAA, 0x90)]])
    last = None
    for pairs in sequences:
        if not pairs:
            continue
        cart_reset(link, profile, address=address)
        before = _read_bytes(link, address, 8)
        link.write_seq(pairs)
        time.sleep(0.01)
        after = _read_bytes(link, address, 8)
        cart_reset(link, profile, address=address)
        last = after
        if profile is not None or after != before:
            return after
    return last or b""


def _cfi_pairs(profile):
    if profile is not None and profile.has_command("read_cfi"):
        return profile.command("read_cfi")
    if profile is not None and profile.command_set == "AMD":
        return [(0xAA, 0x98)]
    if profile is not None and profile.command_set in ("INTEL", "SHARP"):
        return [(0, 0x98)]
    return CFI_SEQ


def cart_cfi(link, profile=None):
    """Read the raw CFI response (at least 512 bytes)."""
    address = profile.read_identifier_at if profile else 0
    cart_reset(link, profile, address=address)
    link.write_seq(_cfi_pairs(profile))
    time.sleep(0.05)
    data = _read_bytes(link, address, CFI_BYTES)
    cart_reset(link, profile, address=address)
    return data


def read_cfi(link, profile=None):
    return parse_cfi(cart_cfi(link, profile))


def _resolve_address(token, base):
    if isinstance(token, int):
        return token
    for prefix in ("SA+", "PA+"):
        if isinstance(token, str) and token.startswith(prefix):
            return base + int(token[len(prefix):], 0)
    if token in ("SA", "PA"):
        return base
    raise ProfileError("unknown address token %r" % (token,))


def _resolve_value(token, buffer_size=None, program_data=None):
    if isinstance(token, int):
        return token & 0xFFFF
    if token == "BS":
        if not buffer_size:
            raise ProfileError("BS token without a buffer size")
        return (buffer_size - 1) & 0xFFFF
    if token == "PD":
        if program_data is None:
            raise ProfileError("PD token without program data")
        return program_data & 0xFFFF
    raise ProfileError("unknown data token %r" % (token,))


def _resolve_wait_address(token, base):
    # Wait rows use numeric offsets from the operation address.  Commands use
    # numbers as absolute cartridge addresses.
    if isinstance(token, int):
        return base + token
    return _resolve_address(token, base)


def _poll_wait(link, rows, base, program_data=None, timeout=20.0):
    """Poll each non-null ``*_wait_for`` row until it matches."""
    for row in rows:
        if not row or row[0] is None:
            continue
        address = _resolve_wait_address(row[0], base)
        expected = row[1]
        mask = row[2] if len(row) > 2 and row[2] is not None else 0xFFFF
        if expected == "PD":
            if program_data is None:
                raise ProfileError("wait-for PD without program data")
            expected = program_data & 0xFFFF
        deadline = time.monotonic() + timeout
        last = None
        while True:
            last = link.read_words(address, 1)[0] & 0xFFFF
            if (last & mask) == expected:
                break
            if time.monotonic() >= deadline:
                raise ProtocolError(
                    "status 0x%04X at 0x%X did not match 0x%04X & 0x%04X "
                    "within %.1fs"
                    % (last, address, expected, mask, timeout))
            time.sleep(0.02)


def _single_write_waits(profile):
    """Wait rows for single-word programming, with a status fallback."""
    return single_write_waits(
        profile.command("single_write"),
        profile.command("single_write_wait_for"),
        profile.command_set)


def _run_sequence(link, commands, waits, base, buffer_size=None,
                  program_data=None, timeout=20.0):
    """Send a command sequence one row at a time, polling after each row.

    Profile wait tables are aligned with their command table, so a wait after
    an early command (for example the first unlock write on some Intel parts)
    must happen before the next command is issued.
    """
    for index, row in enumerate(commands):
        address = _resolve_address(row[0], base)
        value = _resolve_value(row[1], buffer_size, program_data)
        link.write_seq([(address, value)])
        if index < len(waits):
            _poll_wait(link, [waits[index]], base,
                       program_data=program_data, timeout=timeout)


def sector_erase(link, address, profile=None, timeout=30.0):
    if profile is None:
        pairs = [(address, 0x60), (address, 0xD0),
                 (address, 0x20), (address, 0xD0)]
        rows = [[None, None, None]] * 3 + [["SA", 0x80, 0xFFFF]]
    else:
        pairs = profile.command("sector_erase")
        rows = profile.command("sector_erase_wait_for")
    _run_sequence(link, pairs, rows, address, timeout=timeout)
    cart_reset(link, profile, address=address)
    value = link.read_words(address, 1)[0] & 0xFFFF
    if value != 0xFFFF:
        raise ProtocolError(
            "sector 0x%X erased but reads back 0x%04X" % (address, value))
    return True


def chip_erase(link, profile, timeout=300.0):
    """Erase the whole chip with the profile's chip-erase commands."""
    if profile is None or not profile.has_command("chip_erase"):
        raise ProfileError("the selected profile has no chip-erase command")
    rows = profile.command("chip_erase")
    waits = profile.command("chip_erase_wait_for")
    _run_sequence(link, rows, waits, 0, timeout=timeout)
    cart_reset(link, profile, address=0)
    value = link.read_words(0, 1)[0] & 0xFFFF
    if value != 0xFFFF:
        raise ProtocolError(
            "chip erase completed but the cartridge reads 0x%04X" % value)
    return True


def single_write(link, address, value, profile=None, timeout=10.0):
    if profile is None:
        pairs = [(address, 0x70), (address, 0x10), (address, value)]
        rows = [[None, None, None], [None, None, None],
                ["SA", 0x80, 0x80]]
    else:
        pairs = profile.command("single_write")
        rows = _single_write_waits(profile)
    _run_sequence(link, pairs, rows, address,
                  program_data=value, timeout=timeout)
    cart_reset(link, profile, address=address)
    last = link.read_words(address, 1)[0] & 0xFFFF
    if last != (value & 0xFFFF):
        raise ProtocolError(
            "write 0x%04X to 0x%X did not verify (read 0x%04X)"
            % (value, address, last))
    return True


# -- FlashGBX profile autodetection -------------------------------------------

class Detection:
    """Result of matching a cartridge against the loaded profiles."""

    def __init__(self, profile=None, flash_id=None, cfi=None, candidates=None,
                 ambiguous=False, baseline=None, id_buffer=None, method=None):
        self.profile = profile
        self.flash_id = flash_id
        self.cfi = cfi
        self.candidates = list(candidates or [])
        self.ambiguous = ambiguous
        self.baseline = baseline
        self.id_buffer = id_buffer
        self.method = method


def _flashcart(profile, link, verbose=False):
    """Build a FlashGBX Flashcart whose bus calls go over the payload."""
    try:
        from FlashGBX.Flashcart import Flashcart
    except ImportError as error:  # pragma: no cover - environment dependent
        raise ProfileError(
            "FlashGBX is required for cartridge detection: %s" % error)

    def read(address, length=0):
        if length == 0:
            length = 2
        return bytearray(_read_bytes(link, address, length))

    def write(address, value, flashcart=False, sram=False):
        if sram:
            raise ProfileError("device detection does not use save space")
        link.write_seq([(address, value)])

    def write_fast(commands, flashcart=False):
        pairs = []
        for row in commands:
            if not isinstance(row[0], int) or not isinstance(row[1], int):
                raise ProfileError(
                    "device detection cannot relocate %r" % (row,))
            pairs.append((row[0], row[1]))
        if pairs:
            link.write_seq(pairs)

    def power_cycle():
        raise ProfileError("profile needs a cartridge power cycle")

    return Flashcart(config=profile.spec, fncptr={
        "cart_write_fncptr": write,
        "cart_write_fast_fncptr": write_fast,
        "cart_read_fncptr": read,
        "cart_powercycle_fncptr": power_cycle,
        "progress_fncptr": lambda _args: None,
        "set_we_pin_wr": lambda: None,
        "set_we_pin_audio": lambda: None,
    })


def _cfi_matches(profile, cfi):
    if cfi is None:
        return True
    if profile.flash_size and cfi.get("size") \
            and profile.flash_size != cfi["size"]:
        return False
    if (profile.spec.get("sector_size_from_cfi") is True
            and cfi.get("regions")):
        regions = profile.cfi_regions(cfi)
        if regions and regions != cfi_sector_regions(cfi):
            return False
    return True


# -- Burn2Slot fallback detection -------------------------------------------
#
# Not every cart has a FlashGBX profile, and some boards scramble the address
# or data lines. Burn2Slot (MIT, vrodin) finds those chips by trying the six
# common AMD unlock layouts and six data wirings. This is a Python port of the
# scan in Cart Doctor's web/gba_flash.js, which in turn came from that work.

B2S_SEQS = [
    [0x555, 0x2AA, 0x555, 0x555, 0x2AA, 0x555],
    [0xAAA, 0x555, 0xAAA, 0xAAA, 0x555, 0xAAA],
    [0xAAA, 0x554, 0xAAA, 0xAAA, 0x554, 0xAAA],
    [0x5555, 0x2AAA, 0x5555, 0x5555, 0x2AAA, 0x5555],
    [0xAAAA, 0x5554, 0xAAAA, 0xAAAA, 0x5554, 0xAAAA],
    [0x1554, 0xAAA, 0x1554, 0x1554, 0xAAA, 0x1554],
]
B2S_BUS1 = [15, 7, 14, 6, 13, 5, 12, 4, 0, 8, 1, 9, 2, 10, 3, 11]
B2S_BUS2 = [0, 8, 1, 9, 6, 14, 7, 15, 2, 10, 3, 11, 4, 12, 5, 13]
B2S_WIRING_NAMES = [
    "straight", "D0/D1 swapped", "data lines reordered (type 1)",
    "data lines reordered (type 2)", "8-bit, doubled",
    "8-bit, doubled, D0/D1 swapped",
]


def _b2s_swap_to(bus, value):
    result = 0
    for bit in range(16):
        result |= ((value >> bit) & 1) << bus[bit]
    return result


def _b2s_swap_from(bus, value):
    result = 0
    for bit in range(16):
        result |= ((value >> bus[bit]) & 1) << bit
    return result


def _b2s_out(bus, value):
    value &= 0xFFFF
    if bus == 1:
        return swap_bits(value, (0, 1))
    if bus == 2:
        return _b2s_swap_to(B2S_BUS1, value)
    if bus == 3:
        return _b2s_swap_to(B2S_BUS2, value)
    if bus == 4:
        return ((value << 8) | value) & 0xFFFF
    if bus == 5:
        return swap_bits(((value << 8) | value) & 0xFFFF, (0, 1))
    return value


def _b2s_in(bus, value):
    value &= 0xFFFF
    if bus in (1, 5):
        return swap_bits(value, (0, 1))
    if bus == 2:
        return _b2s_swap_from(B2S_BUS1, value)
    if bus == 3:
        return _b2s_swap_from(B2S_BUS2, value)
    return value


def _b2s_amd_spec(sequence, bus, cfi, flash_id):
    addresses = [word * 2 for word in B2S_SEQS[sequence]]
    out = lambda value: _b2s_out(bus, value)
    spec = {
        "type": "AGB",
        "names": ["AMD-style flash, %s (Burn2Slot detection)"
                  % B2S_WIRING_NAMES[bus]],
        "command_set": "AMD",
        "synthetic": True,
        "flash_ids": [list(flash_id)],
        "commands": {
            "reset": [[addresses[0], out(0xAA)],
                      [addresses[1], out(0x55)],
                      [addresses[2], out(0xF0)]],
            "read_identifier": [[addresses[0], out(0xAA)],
                                [addresses[1], out(0x55)],
                                [addresses[2], out(0x90)]],
            "sector_erase": [[addresses[0], out(0xAA)],
                             [addresses[1], out(0x55)],
                             [addresses[2], out(0x80)],
                             [addresses[3], out(0xAA)],
                             [addresses[4], out(0x55)],
                             ["SA", out(0x30)]],
            "sector_erase_wait_for": [
                [None, None, None], [None, None, None], [None, None, None],
                [None, None, None], [None, None, None], ["SA", 0xFFFF, 0xFFFF]],
            "single_write": [[addresses[0], out(0xAA)],
                             [addresses[1], out(0x55)],
                             [addresses[2], out(0xA0)],
                             ["PA", "PD"]],
            "single_write_wait_for": [
                [None, None, None], [None, None, None],
                [None, None, None], [None, None, None]],
        },
    }
    regions = [list(row) for row in cfi_sector_regions(cfi)] if cfi else []
    if regions:
        spec["sector_size"] = regions
        spec["flash_size"] = cfi["size"]
    else:
        # No usable CFI sector table: erase in 64 KiB steps like Burn2Slot.
        # A larger real sector may be erased twice, which is harmless before
        # programming starts. The count covers the GBA's 32 MiB window;
        # sector_map stops at the image length, and total_size stays unknown
        # unless the chip size was reported.
        spec["sector_size"] = [[0x10000, 0x200]]
        if cfi:
            spec["flash_size"] = cfi["size"]
    return spec


def _b2s_intel_spec(profiles, flash_id, cfi):
    generic = None
    for profile in profiles:
        if profile.path is not None and \
                profile.path.stem == "fc_AGB_Generic_0_90":
            generic = profile
            break
    if generic is None:
        return None
    spec = copy.deepcopy(generic.spec)
    spec["names"] = ["Intel-style flash (Burn2Slot detection)"]
    spec["synthetic"] = True
    spec["flash_ids"] = [list(flash_id)]
    if cfi:
        spec["flash_size"] = cfi["size"]
        regions = [list(row) for row in cfi_sector_regions(cfi)]
        if regions:
            spec["sector_size"] = regions
    return spec


def _fallback_detect(link, profiles, verbose=False):
    original = _read_bytes(link, 0, 4)

    # Intel-style chips are tried first, as Burn2Slot does; their reset is a
    # single 0xFF and the ID command is harmless on most other carts.
    try:
        link.write_seq([(0, 0xFF), (0, 0x50), (0, 0x90)])
        intel_id = _read_bytes(link, 0, 4)
        link.write_seq([(0, 0xFF)])
    except ProtocolError:
        intel_id = b""
    if len(intel_id) == 4 and intel_id != original:
        maker = intel_id[0] | (intel_id[1] << 8)
        device = intel_id[2] | (intel_id[3] << 8)
        if (device in (0x8902, 0x8904, 0x887D, 0x887E)
                or (device >> 8) == 0x88
                or (device == 0xB0 and maker == 0xB0)
                or maker == 0x1C):
            cfi = None
            spec = _b2s_intel_spec(profiles, intel_id, None)
            if spec is not None:
                profile = Profile(spec)
                try:
                    cfi = read_cfi(link, profile)
                except ProtocolError:
                    cfi = None
                if cfi:
                    profile = Profile(_b2s_intel_spec(profiles, intel_id, cfi))
                if verbose:
                    print("Burn2Slot: Intel-style ID %s"
                          % intel_id.hex(" ").upper())
                return Detection(profile=profile, flash_id=intel_id, cfi=cfi,
                                 candidates=[profile], id_buffer=intel_id,
                                 method="Burn2Slot Intel scan")

    for sequence in range(len(B2S_SEQS)):
        for bus in range(6):
            out = lambda value: _b2s_out(bus, value)
            resets = [(0, _b2s_out(wiring, 0xF0)) for wiring in range(6)]
            unlock = [
                (B2S_SEQS[sequence][0] * 2, out(0xAA)),
                (B2S_SEQS[sequence][1] * 2, out(0x55)),
                (B2S_SEQS[sequence][2] * 2, out(0x90)),
            ]
            try:
                link.write_seq(resets)
                link.write_seq(unlock)
                raw = _read_bytes(link, 0, 4)
                link.write_seq([(0, out(0xF0))])
            except ProtocolError:
                continue
            if len(raw) < 4 or raw == original:
                continue
            device = _b2s_in(bus, raw[2] | (raw[3] << 8))
            if not ((device >> 8) == 0x22 or device in (0x7E7E, 0x57, 0xF8)):
                continue

            cfi = None
            try:
                link.write_seq([(0, out(0x50)), (0, out(0xFF)),
                                (0xAA, out(0x98))])
                encoded = _read_bytes(link, 0, CFI_BYTES)
                link.write_seq([(0, out(0xF0))])
                decoded = bytearray(len(encoded))
                for index in range(0, len(encoded) - 1, 2):
                    value = _b2s_in(bus, encoded[index]
                                    | (encoded[index + 1] << 8))
                    decoded[index] = value & 0xFF
                    decoded[index + 1] = value >> 8
                cfi = parse_cfi(bytes(decoded))
            except ProtocolError:
                cfi = None
            spec = _b2s_amd_spec(sequence, bus, cfi, raw)
            profile = Profile(spec)
            if verbose:
                print("Burn2Slot: AMD-style ID %s, %s"
                      % (raw.hex(" ").upper(), B2S_WIRING_NAMES[bus]))
            return Detection(profile=profile, flash_id=raw, cfi=cfi,
                             candidates=[profile], id_buffer=raw,
                             method="Burn2Slot AMD scan")
    return None


def detect(link, profiles, verbose=False, fallback=False):
    """Match the cartridge against the loaded profiles with FlashGBX."""
    candidates = []
    for profile in profiles:
        if not profile.is_autodetectable():
            continue
        flashcart = _flashcart(profile, link, verbose=verbose)
        try:
            verified, flash_id = flashcart.VerifyFlashID()
        except Exception as error:
            # A profile whose command sequence this payload cannot run should
            # not abort detection of the others.
            if verbose:
                print("%s: %s" % (profile.primary_name, error))
            continue
        if verbose:
            print("probe %s: %s" % (profile.primary_name,
                                    bytes(flash_id).hex(" ")))
        if verified:
            candidates.append((profile, bytes(flash_id), flashcart))

    if not candidates:
        if fallback:
            result = _fallback_detect(link, profiles, verbose=verbose)
            if result is not None:
                return result
        return Detection()

    cfi = None
    for _profile, _flash_id, flashcart in candidates:
        try:
            raw = flashcart.ReadCFI()
        except Exception:
            continue
        if raw:
            cfi = normalize_cfi(raw)
            break

    matching = [entry for entry in candidates if _cfi_matches(entry[0], cfi)]
    if not matching:
        matching = candidates
    profile, flash_id, _ = matching[0]
    ambiguous = len({entry[0].path for entry in matching}) > 1
    # Keep the full identification response for the `id` command; FlashGBX's
    # VerifyFlashID only returns the bytes selected by flash_id_offsets.
    id_buffer = None
    try:
        id_buffer = cart_id(link, profile)
    except ProtocolError:
        pass
    return Detection(profile=profile, flash_id=flash_id, cfi=cfi,
                     candidates=[entry[0] for entry in matching],
                     ambiguous=ambiguous, id_buffer=id_buffer or flash_id)


def resolve_profile(link, args, profiles=None):
    """Return the profile selected by ``--profile`` or by autodetection."""
    if profiles is None:
        profiles = load_profiles(args.config)
    if args.profile:
        profile = find_profile(profiles, args.profile)
        if profile is None:
            raise SystemExit(
                "unknown profile %r; run `flash.py profiles` to list names"
                % args.profile)
        if not profile.supports_write() and args.command in ("image", "erase",
                                                              "program"):
            raise SystemExit(
                "profile %r cannot write this cartridge (command set %s)"
                % (profile.primary_name, profile.command_set or "unknown"))
        print("profile: %s" % profile.primary_name)
        cfi = None
        if args.command == "image":
            try:
                cfi = read_cfi(link, profile)
            except ProtocolError as error:
                print("CFI: not readable (%s)" % error)
        return profile, cfi

    result = detect(link, profiles, verbose=args.verbose, fallback=True)
    if result.profile is None:
        raise SystemExit(
            "cartridge not found in the loaded profiles, and the Burn2Slot "
            "scan did not recognise it either; pass --profile NAME "
            "(run `flash.py profiles`)")
    if (result.profile.spec.get("synthetic")
            and args.command in ("image", "erase", "program")
            and not getattr(args, "fallback_write", False)):
        raise SystemExit(
            "detection used the Burn2Slot fallback; writing an unverified "
            "cart type needs --fallback-write. Back up the cartridge first.")
    if result.ambiguous and args.verbose:
        names = ", ".join(profile.primary_name for profile in result.candidates)
        print("warning: several profiles matched, using %s (%s)"
              % (result.profile.primary_name, names))
    if (args.command in ("image", "erase", "program")
            and not result.profile.supports_write()):
        raise SystemExit(
            "profile %r cannot write this cartridge (command set %s)"
            % (result.profile.primary_name,
               result.profile.command_set or "unknown"))
    if result.method:
        print("detection: %s" % result.method)
    print("profile: %s" % result.profile.primary_name)
    return result.profile, result.cfi



SAVE_BASE = 0x0E000000
SAVE_SIZE = 0x10000  # 64 KiB SRAM, the save media on this cartridge
SAVE_TYPES = {
    "sram-32k": 0x8000,
    "sram-64k": 0x10000,
    "sram-128k": 0x20000,
}


def save_dump(link, output, size=SAVE_SIZE):
    """Read the cartridge save media and write it to a raw .sav file."""
    if size <= 0:
        raise ValueError("save size must be a positive byte count")
    data = read_bytes(link, SAVE_BASE, size)
    Path(output).write_bytes(data)
    return data


def save_restore(link, path, size=SAVE_SIZE, verify=True, show_progress=True):
    """Write a raw .sav file back to the cartridge save media."""
    data = Path(path).read_bytes()
    if not data:
        raise ProtocolError("save file is empty")
    if len(data) != size:
        raise ProtocolError(
            "save file is %d bytes but the cartridge save is exactly %d "
            "bytes; a partial write would leave the rest of the save stale"
            % (len(data), size))
    written = 0
    for offset in range(0, len(data), BLOCK_BYTES):
        block = data[offset:offset + BLOCK_BYTES]
        link.write8(SAVE_BASE + offset, block)
        written += len(block)
        if show_progress:
            progress(link, written * 100 // len(data))
            print("\rwrote %d of %d bytes" % (written, len(data)),
                  end="", flush=True)
    if show_progress:
        print()
    if not verify:
        return data
    back = read_bytes(link, SAVE_BASE, len(data))
    back = bytes(back)
    if back != data:
        offset = next(position for position in range(len(data))
                      if back[position] != data[position])
        raise ProtocolError(
            "save verify failed at 0x%X (expected %02X, got %02X)"
            % (offset, data[offset], back[offset]))
    return data


def encode_write_plan(profile, buffer_size=None):
    """Encode a profile's write template for the payload."""
    return plan_encode_write_plan(
        profile.commands, buffer_size,
        command_set=profile.command_set, name=profile.primary_name)


def prepare_write(link, profile, cfi=None):
    """Install a profile's write plan and return the payload block size."""
    buffer_size = profile.buffer_size(cfi)
    return plan_prepare_write(
        link, profile.commands, buffer_size,
        command_set=profile.command_set, name=profile.primary_name)


def _crc32(data):
    return zlib.crc32(data) & 0xFFFFFFFF


def _sector_crcs(image, sectors):
    """CRC32 of each sector of the image, as the payload reports them."""
    return [_crc32(image[offset:offset + size]) for offset, size in sectors]


def _probe_points(offset, size):
    """Three byte offsets sampled in a sector: start, middle, end."""
    if size <= 0:
        return []
    points = []
    for point in (offset, offset + size // 2,
                  offset + max(0, size - PROBE_BYTES)):
        point = min(max(point, offset), offset + size - 1)
        if point not in points:
            points.append(point)
    return sorted(points)


def _probe_spec(sectors):
    """Regions read to tell whether the cartridge changed since the state.

    The old 4 KiB fingerprint of sector 0 missed every change after the first
    sector.  Sampling every sector costs one small read per point but covers
    the whole image.
    """
    spec = []
    for offset, size in sectors:
        for point in _probe_points(offset, size):
            spec.append([point, min(PROBE_BYTES, offset + size - point)])
    return spec


def _probe_digest(link, spec):
    digest = hashlib.sha256()
    for offset, length in spec:
        digest.update(read_region(link, offset, length))
    return digest.hexdigest()


def _state_key(profile, sectors):
    name = "-".join(part for part in "".join(
        character if character.isalnum() else "-"
        for character in profile.primary_name).lower().split("-") if part)
    digest = hashlib.sha256(json.dumps(
        [[offset, size] for offset, size in sectors]).encode()).hexdigest()
    return "%s-%s" % (name[:48] or "cart", digest[:8])


def default_state_path(profile, sectors):
    """A cache file per cartridge profile and sector map.

    The state used to live next to the script, so two cartridges, or a
    read-only install, either invalidated each other or failed to save.
    """
    root = os.environ.get(STATE_DIR_ENV)
    if not root:
        root = str(Path.home() / ".cache" / "flashgblink" / "delta")
    return Path(root) / (_state_key(profile, sectors) + ".json")


def _load_state(path, sectors, size, probe_spec):
    try:
        state = json.loads(Path(path).read_text())
    except Exception:
        return None
    if state.get("version") != STATE_VERSION or state.get("size") != size:
        return None
    if state.get("sector_map") != [[offset, size_]
                                   for offset, size_ in sectors]:
        return None
    if state.get("probe_spec") != probe_spec:
        return None
    if (not isinstance(state.get("crc32"), list)
            or len(state["crc32"]) != len(sectors)):
        return None
    return state


def _save_state(path, image, sectors, image_crcs, probe_spec, link):
    state = {
        "version": STATE_VERSION,
        "sector_map": [[offset, size] for offset, size in sectors],
        "size": len(image),
        "crc32": image_crcs,
        "probe_spec": probe_spec,
        "probe": _probe_digest(link, probe_spec),
    }
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(state, indent=1))
    except OSError as error:
        print("warning: could not save the delta state to %s (%s)"
              % (path, error))


def _crc_supported(link):
    try:
        link.crc32(0, 4)
    except UnsupportedCommand:
        return False
    return True


def _verify_sector(link, profile, start, chunk, use_crc=False, attempts=3):
    """Return None if the sector matches, else the read-back bytes."""
    cart_reset(link, profile, address=start)
    if use_crc:
        expected = _crc32(chunk)
        for attempt in range(attempts):
            if link.crc32(start, len(chunk)) == expected:
                return None
            if attempt + 1 < attempts:
                print(" retry ...", end="", flush=True)
                time.sleep(0.05)
        back = read_region(link, start, len(chunk))
        # The CRC is the cheap check; a byte comparison settles a disagreement
        # and keeps the error message able to name the first differing byte.
        return None if back == chunk else back
    back = None
    for attempt in range(attempts):
        back = read_region(link, start, len(chunk))
        if back == chunk:
            return None
        if attempt + 1 < attempts:
            print(" retry ...", end="", flush=True)
            time.sleep(0.05)
    return back


def _program_sector(link, profile, start, chunk, block_bytes, cancelled=None):
    cart_reset(link, profile, address=start)
    sector_erase(link, start, profile)
    written = 0
    for offset in range(0, len(chunk), block_bytes):
        if cancelled is not None and cancelled():
            raise KeyboardInterrupt("write cancelled")
        block = chunk[offset:offset + block_bytes]
        if block.count(0xFF) == len(block):
            continue
        write_block(link, start + offset, block)
        written += len(block)
    return written


def write_image(link, path, profile, cfi=None, full=False, verify=True,
                boot=False, state_path=None, erase_chip=False):
    image = Path(path).read_bytes()
    if not image:
        raise ProtocolError("image is empty")
    if profile is None:
        raise ProfileError("writing an image needs a flashcart profile")
    if profile.spec.get("flash_bank_select_type"):
        raise ProfileError(
            "profile %r needs flash bank switching, which this tool cannot do"
            % profile.primary_name)
    if profile.command_set in ("3DMEMORY", "GBAMP", "SINTAX_YJENCRYPTED",
                               "VASTFAME"):
        raise ProfileError(
            "profile %r uses the %s command set, which cannot be written"
            % (profile.primary_name, profile.command_set))

    total = profile.total_size(cfi)
    if total and len(image) > total:
        raise ProtocolError(
            "image is %d bytes but the profile's flash is %d bytes"
            % (len(image), total))
    if total and total > 0x2000000:
        raise ProfileError(
            "flash is %d bytes, beyond the GBA's 32 MiB cartridge window"
            % total)
    padded = profile.pad_to_sector(len(image), cfi)
    if total and padded > total:
        raise ProtocolError(
            "image padded to %d bytes exceeds the profile's %d-byte flash"
            % (padded, total))
    if padded > len(image):
        image += b"\xff" * (padded - len(image))
    block_bytes, chunk_bytes = prepare_write(link, profile, cfi)

    if erase_chip:
        if not profile.has_command("chip_erase"):
            raise ProfileError(
                "profile %r has no chip-erase command; use sector erase"
                % profile.primary_name)
        if state_path is None and total:
            state_path = default_state_path(profile, [(0, total)])
        if state_path is not None:
            Path(state_path).unlink(missing_ok=True)
        print("chip erase: erasing the whole flash")
        chip_erase(link, profile)
        payload = image
        padding = (-len(payload)) % 4
        if padding:
            payload += b"\xff" * padding
        for offset in range(0, len(payload), block_bytes):
            block = payload[offset:offset + block_bytes]
            if block and block.count(0xFF) == len(block):
                continue
            write_block(link, offset, block)
        if verify:
            back = read_region(link, 0, len(image))
            if back != image:
                position = next(index for index in range(len(image))
                                if back[index] != image[index])
                raise ProtocolError(
                    "verify failed at 0x%X after chip erase (expected %02X, "
                    "got %02X)" % (position, image[position], back[position]))
        progress(link, 100)
        done(link)
        print("done")
        if boot:
            link.boot()
        return

    sectors = profile.sector_map(len(image), cfi)
    if not sectors:
        raise ProfileError(
            "profile %r has no sector map and no usable CFI data; "
            "pass --chip-erase if the profile supports it"
            % profile.primary_name)
    sectors = [(offset, size) for offset, size in sectors
               if offset < len(image)]
    if not sectors or sectors[-1][0] + sectors[-1][1] < len(image):
        raise ProfileError("sector map does not cover the whole image")

    image_crcs = _sector_crcs(image, sectors)
    probe_spec = _probe_spec(sectors)
    use_crc = _crc_supported(link)
    if state_path is None:
        state_path = default_state_path(profile, sectors)
    print("image: %s (%d bytes, sha256 %s)"
          % (path, len(image), hashlib.sha256(image).hexdigest()))
    print("sectors: %d, write window: %d bytes"
          % (len(sectors), chunk_bytes))

    state = None if full else _load_state(state_path, sectors, len(image),
                                          probe_spec)
    if state is not None:
        # The state is only a hint: confirm the cartridge still looks like it
        # did when the state was written before trusting it.
        if _probe_digest(link, probe_spec) != state.get("probe"):
            print("cartridge changed outside the tool; checking every sector")
            state = None

    if full:
        dirty = list(range(len(image_crcs)))
        print("full: rewriting all %d sectors" % len(dirty))
    elif state is not None:
        dirty = [index for index, crc in enumerate(image_crcs)
                 if crc != state["crc32"][index]]
        print("state: %d of %d sectors changed" % (len(dirty), len(image_crcs)))
    elif use_crc:
        print("checking %d sectors on the cartridge ..." % len(sectors))
        dirty = []
        for index, ((offset, size), expected) in enumerate(zip(sectors,
                                                               image_crcs)):
            cart_reset(link, profile, address=offset)
            if link.crc32(offset, size) != expected:
                dirty.append(index)
            progress(link, (index + 1) * 100 // len(sectors))
        print("diff: %d of %d sectors changed" % (len(dirty), len(sectors)))
    else:
        print("reading %d bytes from the cartridge ..." % len(image))
        current = bytearray()
        for index, (offset, size) in enumerate(sectors):
            current += read_region(link, offset, size)
            progress(link, (index + 1) * 100 // len(sectors))
        current = bytes(current)
        dirty = [index for index, (offset, size) in enumerate(sectors)
                 if current[offset:offset + size]
                 != image[offset:offset + size]]
        print("diff: %d of %d sectors changed" % (len(dirty), len(sectors)))

    if not dirty:
        print("cartridge already matches the image")
        if state is None:
            _save_state(state_path, image, sectors, image_crcs, probe_spec,
                        link)
        progress(link, 100)
        done(link)
        if boot:
            link.boot()
        return

    # A partial flash must not leave a state file that claims a match.
    Path(state_path).unlink(missing_ok=True)
    progress(link, 0)

    cancelled = {"value": False}

    def _cancel(_signum, _frame):
        cancelled["value"] = True

    previous_handler = signal.signal(signal.SIGINT, _cancel)
    try:
        for number, index in enumerate(dirty, 1):
            if cancelled["value"]:
                link.abort()
                Path(state_path).unlink(missing_ok=True)
                raise KeyboardInterrupt("write cancelled")
            offset, size = sectors[index]
            chunk = image[offset:offset + size]
            print("[%d/%d] sector 0x%X (0x%X bytes) ..."
                  % (number, len(dirty), offset, size), end="", flush=True)
            back = None
            error = None
            for attempt in range(2):
                try:
                    written = _program_sector(
                        link, profile, offset, chunk, block_bytes,
                        cancelled=lambda: cancelled["value"])
                except ProtocolError as failure:
                    # A rejected block leaves the sector half programmed; erase
                    # it again rather than aborting the whole image.
                    error = failure
                    print(" %s, re-erasing ..." % failure, end="", flush=True)
                    continue
                error = None
                print(" wrote %d bytes" % written, end="", flush=True)
                if not verify:
                    break
                back = _verify_sector(link, profile, offset, chunk, use_crc)
                if back is None:
                    print(", verified", end="")
                    break
                print(" mismatch, rewriting ...", end="", flush=True)
            if error is not None:
                raise ProtocolError(
                    "sector 0x%X could not be programmed: %s"
                    % (offset, error))
            if verify and back is not None:
                position = next(position for position in range(len(chunk))
                                if back[position] != chunk[position])
                raise ProtocolError(
                    "verify failed in sector 0x%X at 0x%X (expected %s, got %s)"
                    % (offset, position,
                       chunk[position:position + 8].hex(" "),
                       back[position:position + 8].hex(" ")))
            progress(link, number * 100 // len(dirty))
            print()
    except KeyboardInterrupt:
        Path(state_path).unlink(missing_ok=True)
        try:
            link.abort()
        except Exception:
            pass
        raise
    finally:
        signal.signal(signal.SIGINT, previous_handler)

    if verify:
        _save_state(state_path, image, sectors, image_crcs, probe_spec, link)
    else:
        print("note: --no-verify does not record the delta state; "
              "the next run checks the cartridge")
    progress(link, 100)
    done(link)
    print("done")
    if boot:
        link.boot()



# -- CLI ----------------------------------------------------------------------

def print_cfi(info):
    print("CFI signature: OK")
    print("swapped pins: %s" % (info["pins"] or "none"))
    print("size: 0x%X (%.2f MiB)"
          % (info["size"], info["size"] / (1024 * 1024)))
    print("voltage: %.1f-%.1f V" % (info["vdd_min"], info["vdd_max"]))
    position = 0
    for block, count in cfi_sector_regions(info):
        end = position + block * count
        print("region 0x%07X-0x%07X: 0x%X x %d"
              % (position, end - 1, block, count))
        position = end
    if info.get("boot_sector_raw") == 0x03:
        print("sector map: reversed (top boot)")
    print("buffer write: %s"
          % ("%d bytes" % info["buffer_size"]
             if info.get("buffer_size") else "not reported"))


def print_detection(result):
    if result.profile is None:
        print("no profile matched the cartridge")
        if result.baseline is not None:
            print("  before identification: %s" % result.baseline[:8].hex(" "))
        if result.id_buffer is not None:
            print("  after identification:  %s" % result.id_buffer[:8].hex(" "))
        return 1
    profile = result.profile
    print("profile: %s" % profile.primary_name)
    if result.method:
        print("detection: %s" % result.method)
    if profile.path:
        print("file: %s" % profile.path.name)
    if len(profile.names) > 1:
        print("aliases: %d (run `flash.py profiles`)"
              % (len(profile.names) - 1))
    print("command set: %s" % (profile.command_set or "unknown"))
    print("flash ID: %s" % result.flash_id.hex(" ").upper())
    if result.ambiguous:
        print("warning: %d profiles match this ID; using the first"
              % len(result.candidates))
    if result.cfi:
        print_cfi(result.cfi)
    else:
        print("CFI: not readable with this profile")
    return 0


def profiles_command(args):
    profiles = load_profiles(args.config)
    for profile in profiles:
        suffix = ""
        if len(profile.names) > 1:
            suffix = " (+%d aliases)" % (len(profile.names) - 1)
        if not profile.supports_write():
            suffix += " [read-only]"
        print("%s%s" % (profile.primary_name, suffix))
        if args.verbose:
            for name in profile.names[1:]:
                print("  alias: %s" % name)
            if profile.path:
                print("  file: %s" % profile.path)
    print("%d profiles in %s" % (len(profiles), find_config_dir(args.config)))
    return 0


def upload(args):
    payload = Path(args.payload)
    if not payload.is_file():
        raise SystemExit(
            "%s not found; build it with: make -C %s"
            % (payload, payload.parent))
    print("Waiting for the GBA: hold START+SELECT and power it on.", flush=True)
    command = [args.gbasend, "-p", args.port, "-b", str(args.upload_baud),
               "-m", "--handshake-wait", str(args.handshake_wait),
               str(payload)]
    result = subprocess.run(command, check=False)
    if result.returncode != 0:
        raise SystemExit(
            "payload upload failed (gbasend exit %d). Hold START+SELECT "
            "while powering the GBA on, check the link wiring, then try "
            "again." % result.returncode)


def doctor(args):
    problems = 0

    payload = Path(args.payload)
    if payload.is_file():
        print("payload: ok (%s, %d bytes)" % (payload, payload.stat().st_size))
    else:
        print("payload: MISSING (%s); build it with `make -C %s`"
              % (payload, payload.parent))
        problems += 1

    try:
        profiles = load_profiles(args.config)
        print("profiles: ok (%d in %s)"
              % (len(profiles), find_config_dir(args.config)))
    except ProfileError as error:
        print("profiles: MISSING (%s)" % error)
        problems += 1

    try:
        config_dir = find_config_dir(args.config)
    except ProfileError:
        config_dir = None
    if config_dir is not None:
        plugin = config_dir / "plugins" / "flashgblink_plugin.py"
        if plugin.is_file():
            print("plugin: ok (%s)" % plugin)
        else:
            print("plugin: MISSING (%s); run "
                  "`python3 scripts/install_plugin.py`" % plugin.parent)
            problems += 1

    try:
        import serial
        print("pyserial: ok (%s)" % serial.__version__)
    except ImportError:
        print("pyserial: MISSING; python3 -m pip install pyserial")
        problems += 1

    try:
        import FlashGBX
        print("FlashGBX: ok (%s)" % getattr(FlashGBX, "__version__", "installed"))
    except ImportError:
        print("FlashGBX: MISSING; python3 -m pip install FlashGBX")
        problems += 1

    gbasend = shutil.which(args.gbasend)
    if gbasend is None and Path(args.gbasend).is_file():
        gbasend = args.gbasend
    if gbasend:
        print("gbasend: ok (%s)" % gbasend)
    else:
        print("gbasend: MISSING (%s); build gbasend-rs or pass --gbasend"
              % args.gbasend)
        problems += 1

    try:
        port = find_port(args.port)
        print("link port: ok (%s)" % port)
    except SystemExit as error:
        print("link port: MISSING (%s)" % error)
        problems += 1

    return 0 if problems == 0 else 1


def open_link(args):
    link = Link(args.port, args.baud, verbose=args.verbose)
    link.handshake()
    return link


def main(argv=None):
    # Options are accepted before or after the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--port", default=argparse.SUPPRESS,
                        help="serial port (default: autodetect)")
    common.add_argument("--baud", type=int, default=argparse.SUPPRESS,
                        help="SPI clock in Hz for the data phase")
    common.add_argument("--upload-baud", type=int, default=argparse.SUPPRESS,
                        help="SPI clock in Hz for the multiboot upload")
    common.add_argument("--gbasend", default=argparse.SUPPRESS,
                        help="path to the gbasend CLI")
    common.add_argument("--payload", default=argparse.SUPPRESS,
                        help="payload .gba to upload")
    common.add_argument("--config", default=argparse.SUPPRESS,
                        help="FlashGBX profile directory (default: autodetect)")
    common.add_argument("--profile", default=argparse.SUPPRESS,
                        help="flashcart profile name; skips autodetection")
    common.add_argument("--handshake-wait", type=int, default=argparse.SUPPRESS,
                        help="seconds gbasend waits for the GBA handshake")
    common.add_argument("--verbose", action="store_true",
                        default=argparse.SUPPRESS)

    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     parents=[common])
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("upload", parents=[common],
                   help="upload the payload over multiboot")
    sub.add_parser("doctor", parents=[common],
                   help="check the payload, profiles, gbasend, and port")
    sub.add_parser("profiles", parents=[common],
                   help="list the loaded flashcart profiles")
    sub.add_parser("detect", parents=[common],
                   help="identify the cartridge and select a profile")
    sub.add_parser("id", parents=[common],
                   help="read the cartridge flash ID")
    sub.add_parser("cfi", parents=[common],
                   help="read and decode the cartridge CFI")
    sub.add_parser("boot", parents=[common],
                   help="soft reset into the cartridge")

    read = sub.add_parser("read", parents=[common],
                          help="read cartridge bytes")
    read.add_argument("address", type=lambda value: int(value, 0))
    read.add_argument("length", type=lambda value: int(value, 0))
    read.add_argument("output")

    save = sub.add_parser("save", parents=[common],
                          help="dump the cartridge save to a raw .sav file")
    save.add_argument("output")
    save.add_argument("--size", type=lambda value: int(value, 0),
                      default=SAVE_SIZE,
                      help="save media size in bytes (default 0x10000)")
    save.add_argument("--save-type", choices=sorted(SAVE_TYPES),
                      help="named SRAM size; overrides --size. EEPROM and "
                           "save-flash are not supported")

    restore = sub.add_parser("restore", parents=[common],
                             help="write a raw .sav file to the cartridge save")
    restore.add_argument("file")
    restore.add_argument("--size", type=lambda value: int(value, 0),
                         default=SAVE_SIZE,
                         help="save media size in bytes (default 0x10000)")
    restore.add_argument("--save-type", choices=sorted(SAVE_TYPES),
                         help="named SRAM size; overrides --size. The file "
                              "must match this size exactly")
    restore.add_argument("--no-verify", action="store_true")

    erase = sub.add_parser("erase", parents=[common],
                           help="erase one flash sector")
    erase.add_argument("address", type=lambda value: int(value, 0))
    erase.add_argument("--timeout", type=float, default=30.0)
    erase.add_argument("--fallback-write", action="store_true",
                       help="allow a Burn2Slot-detected cart")

    program = sub.add_parser("program", parents=[common],
                             help="program one 16-bit word")
    program.add_argument("address", type=lambda value: int(value, 0))
    program.add_argument("value", type=lambda value: int(value, 0))
    program.add_argument("--fallback-write", action="store_true",
                         help="allow a Burn2Slot-detected cart")

    seq = sub.add_parser("seq", parents=[common],
                         help="write address:value pairs verbatim")
    seq.add_argument("pairs", nargs="+", metavar="ADDR:VALUE")

    image = sub.add_parser("image", parents=[common],
                           help="write a .gba image, erasing changed sectors")
    image.add_argument("file")
    image.add_argument("--full", action="store_true",
                       help="ignore the diff and state, rewrite every sector")
    image.add_argument("--no-verify", action="store_true")
    image.add_argument("--boot", action="store_true")
    image.add_argument("--fallback-write", action="store_true",
                       help="allow writing when detection used the Burn2Slot "
                            "fallback instead of a known FlashGBX profile")
    image.add_argument("--chip-erase", action="store_true",
                       help="erase the whole chip before programming instead "
                            "of erasing sectors")
    image.add_argument("--state", default=None,
                       help="delta state file (default: a cache file keyed by "
                            "the profile and sector map; "
                            "FLASHGBLINK_STATE_DIR changes the directory)")

    args = parser.parse_args(argv)
    # The shared parent actions use SUPPRESS so the subparser cannot overwrite
    # a value already given before the subcommand; fill the defaults here.
    defaults = {
        "port": None,
        "baud": 1_800_000,
        "upload_baud": 256_000,
        "gbasend": "gbasend",
        "payload": str(PAYLOAD),
        "config": None,
        "profile": None,
        "handshake_wait": 60,
        "verbose": False,
    }
    for key, value in defaults.items():
        if not hasattr(args, key):
            setattr(args, key, value)

    if args.command == "doctor":
        return doctor(args)

    if args.command == "profiles":
        return profiles_command(args)

    if args.command == "upload":
        args.port = find_port(args.port)
        upload(args)
        return 0

    profiles = load_profiles(args.config)
    args.port = find_port(args.port)
    link = open_link(args)
    try:
        if args.command == "detect":
            if args.profile:
                profile = find_profile(profiles, args.profile)
                if profile is None:
                    raise SystemExit("unknown profile %r" % args.profile)
                print("profile: %s" % profile.primary_name)
                print("flash ID: %s"
                      % cart_id(link, profile).hex(" ").upper())
                try:
                    print_cfi(read_cfi(link, profile))
                except ProtocolError as error:
                    print("CFI: not readable (%s)" % error)
                return 0
            return print_detection(detect(link, profiles,
                                          verbose=args.verbose, fallback=True))
        elif args.command == "id":
            if args.profile:
                profile = find_profile(profiles, args.profile)
                if profile is None:
                    raise SystemExit("unknown profile %r" % args.profile)
                print(cart_id(link, profile).hex(" ").upper())
                print("profile: %s" % profile.primary_name)
            else:
                result = detect(link, profiles, verbose=args.verbose,
                                fallback=True)
                if result.profile is not None:
                    print(result.id_buffer[:8].hex(" ").upper())
                    print("profile: %s" % result.profile.primary_name)
                else:
                    print(cart_id(link).hex(" ").upper())
                    sys.stderr.write(
                        "warning: no profile matched the flash ID\n")
        elif args.command == "cfi":
            profile = None
            detected = None
            if args.profile:
                profile = find_profile(profiles, args.profile)
                if profile is None:
                    raise SystemExit("unknown profile %r" % args.profile)
            else:
                detected = detect(link, profiles, verbose=args.verbose,
                                  fallback=True)
                profile = detected.profile
            if profile is not None:
                info = (detected.cfi if detected is not None and detected.cfi
                        else read_cfi(link, profile))
            else:
                try:
                    info = read_cfi(link)
                except ProtocolError:
                    cart_reset(link)
                    link.write_seq([(0, 0x98)])
                    time.sleep(0.05)
                    info = parse_cfi(_read_bytes(link, 0, CFI_BYTES))
                    cart_reset(link)
            print_cfi(info)
        elif args.command == "read":
            # Read in link-sized chunks: a single multi-megabyte frame blocks
            # the host's serial write while the Pico drains it, and leaves the
            # payload inside the read command waiting for words that never come.
            data = read_region(link, args.address, args.length)
            Path(args.output).write_bytes(data)
            print("wrote %d bytes to %s" % (args.length, args.output))
        elif args.command == "save":
            size = SAVE_TYPES.get(args.save_type, args.size)
            data = save_dump(link, args.output, size)
            print("wrote %d bytes to %s (sha256 %s)"
                  % (len(data), args.output, hashlib.sha256(data).hexdigest()))
        elif args.command == "restore":
            size = SAVE_TYPES.get(args.save_type, args.size)
            data = save_restore(link, args.file, size,
                                verify=not args.no_verify)
            print("%d bytes %s from %s"
                  % (len(data),
                     "written" if args.no_verify else "written and verified",
                     args.file))
        elif args.command == "erase":
            profile, _ = resolve_profile(link, args, profiles)
            if sector_erase(link, args.address, profile, args.timeout):
                print("sector 0x%X erased" % args.address)
        elif args.command == "program":
            profile, _ = resolve_profile(link, args, profiles)
            single_write(link, args.address, args.value, profile)
            print("0x%04X programmed at 0x%X" % (args.value, args.address))
        elif args.command == "seq":
            pairs = []
            for item in args.pairs:
                address, value = item.split(":")
                pairs.append((int(address, 0), int(value, 0)))
            link.write_seq(pairs)
            print("wrote %d pairs" % len(pairs))
        elif args.command == "image":
            profile, cfi = resolve_profile(link, args, profiles)
            write_image(link, args.file, profile, cfi=cfi,
                        full=args.full, verify=not args.no_verify,
                        boot=args.boot, state_path=args.state,
                        erase_chip=args.chip_erase)
        elif args.command == "boot":
            link.boot()
            print("soft reset requested")
    finally:
        link.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ProtocolError, KeyboardInterrupt) as error:
        sys.stderr.write("error: %s\n" % error)
        sys.exit(1)
