#!/usr/bin/env python3
"""Flash a GBA cartridge through the link port.

Uploads ``payload/flasher.gba`` into the GBA with gbasend, then talks to it over
the same gbasend-pico USB serial bridge.  The GBA payload only performs 16-bit
cartridge stores/loads and a soft reset; every flash command sequence is
composed here, so the payload does not need to be rebuilt for a new cartridge.

Quick start (cart inserted, GBA off):

    python3 tools/gba_flasher/flash.py upload      # hold START+SELECT, power on
    python3 tools/gba_flasher/flash.py id
    python3 tools/gba_flasher/flash.py cfi

Requires pyserial and the ``gbasend`` CLI from
https://github.com/nullstalgia/gbasend-rs.
"""

import argparse
import hashlib
import json
import shutil
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path


def _require_serial():
    try:
        import serial
        from serial.tools import list_ports
    except ImportError:  # pragma: no cover - environment dependent
        raise SystemExit(
            "pyserial is required: python3 -m pip install pyserial")
    return serial, list_ports

GBASEND_VID = 0x2E8A
GBASEND_PID = 0x000A

PING = 0x5A5A5A5A
HDR_MAGIC = 0x5A

OP_WRITE_SEQ = 0x20
OP_READ = 0x30
OP_BOOT = 0x40

ST_OK = 0xA5A50000
ST_BAD = 0xA5A500E1

PAYLOAD = Path(__file__).resolve().parent / "payload" / "flasher.gba"

# -- M36L0R8060B / Intel command set, from FlashGBX fc_AGB_M36L0R806.txt --
ID_SEQ = [(0, 0x90)]
CFI_SEQ = [(0xAA, 0x98)]
SECTOR_ERASE_VALUES = (0x60, 0xD0, 0x20, 0xD0)

OP_WRITE_BLOCK = 0x50
BLOCK_BYTES = 4096
SECTOR_BYTES = 0x20000
RESET_EVERY = 0x100000
STATE_PATH = Path(__file__).resolve().parent / ".flash-state.json"
FINGERPRINT_BYTES = 4096


class ProtocolError(RuntimeError):
    pass


def find_port(explicit=None):
    if explicit:
        return explicit
    _, list_ports = _require_serial()
    for info in list_ports.comports():
        if info.vid == GBASEND_VID and info.pid == GBASEND_PID:
            return info.device
    raise SystemExit(
        "gbasend-pico serial port not found; pass --port or connect the device")


def _pack_words(words):
    # The GBA's SIO shifts each 32-bit word MSB-first, so the serial byte
    # order is big-endian for both directions.
    return b"".join(struct.pack(">I", word & 0xFFFFFFFF) for word in words)


class Link:
    """Word exchange over the gbasend-pico transparent SPI bridge."""

    def __init__(self, port, baud, verbose=False):
        serial, _ = _require_serial()
        self.verbose = verbose
        self.ser = serial.Serial(port, baudrate=baud, timeout=0.05,
                                 write_timeout=5)
        self._buffer = bytearray()
        self._cond = threading.Condition()
        self._closed = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self):
        while not self._closed:
            try:
                data = self.ser.read(4096)
            except Exception:
                break
            if data:
                with self._cond:
                    self._buffer += data
                    self._cond.notify_all()

    def close(self):
        self._closed = True
        try:
            self.ser.close()
        except Exception:
            pass

    def xfer(self, words, timeout=5.0):
        frames = _pack_words(words)
        expected = len(words) * 4
        with self._cond:
            self._buffer.clear()
        self.ser.write(frames)
        self.ser.flush()
        deadline = time.monotonic() + timeout
        with self._cond:
            while len(self._buffer) < expected:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ProtocolError(
                        "timeout waiting for %d reply bytes (got %d)"
                        % (expected, len(self._buffer)))
                self._cond.wait(remaining)
            raw = bytes(self._buffer[:expected])
            del self._buffer[:expected]
        result = list(struct.unpack(">%dI" % len(words), raw))
        if self.verbose:
            print("tx", _hex(words))
            print("rx", _hex(result))
        return result

    def ping(self, timeout=5.0):
        return self.xfer([PING], timeout=timeout)[0]

    def handshake(self, timeout=8.0):
        """Wait for the payload to answer after a multiboot upload."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                word = self.ping(timeout=0.5)
            except ProtocolError:
                time.sleep(0.05)
                continue
            if (word & 0xFFFF0000) == 0xA5A50000:
                return word
            time.sleep(0.05)
        raise ProtocolError(
            "no payload response; upload it with `flash.py upload` first")

    def write_seq(self, pairs, timeout=5.0):
        for start in range(0, len(pairs), 200):
            chunk = pairs[start:start + 200]
            words = [(HDR_MAGIC << 24) | (OP_WRITE_SEQ << 8) | len(chunk)]
            for address, value in chunk:
                words.append(address & 0xFFFFFFFF)
                words.append(value & 0xFFFF)
            words.append(PING)
            status = self.xfer(words, timeout=timeout)[-1]
            _check_status(status)

    def read_words(self, address, count, timeout=10.0):
        words = [(HDR_MAGIC << 24) | (OP_READ << 8), address & 0xFFFFFFFF,
                 count & 0xFFFFFFFF]
        words += [0] * count
        words.append(PING)
        replies = self.xfer(words, timeout=timeout)
        _check_status(replies[-1])
        return replies[3:3 + count]

    def boot(self):
        self.ser.write(_pack_words([(HDR_MAGIC << 24) | (OP_BOOT << 8)]))
        self.ser.flush()


def _check_status(word):
    if word == ST_BAD:
        raise ProtocolError("payload rejected command (0x%08X)" % word)
    if (word & 0xFFFFFF00) == ST_OK:
        return
    raise ProtocolError("unexpected status 0x%08X" % word)


def _hex(words):
    return " ".join("%08X" % word for word in words)


# -- cartridge operations -----------------------------------------------------

def cart_reset(link, address=0):
    # Intel command state is per bank; reset in the bank being accessed.
    link.write_seq([(address, 0x50), (address, 0xFF)])


def cart_id(link):
    cart_reset(link)
    link.write_seq(ID_SEQ)
    time.sleep(0.01)
    words = link.read_words(0, 2)
    cart_reset(link)
    return b"".join(struct.pack("<I", word) for word in words)


def cart_cfi(link):
    link.write_seq(CFI_SEQ)
    time.sleep(0.05)
    words = link.read_words(0, 0x80)
    cart_reset(link)
    return b"".join(struct.pack("<I", word) for word in words)


def sector_erase(link, address, timeout=12.0):
    link.write_seq([(address, value) for value in SECTOR_ERASE_VALUES])
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        # Intel status register: SR.7 set while the erase is in progress.
        last = link.read_words(address, 1)[0] & 0xFFFF
        if last == 0x80:
            cart_reset(link, address)
            if (link.read_words(address, 1)[0] & 0xFFFF) == 0xFFFF:
                return True
            raise ProtocolError(
                "sector 0x%X erased but reads back 0x%04X"
                % (address, link.read_words(address, 1)[0] & 0xFFFF))
        time.sleep(0.05)
    raise ProtocolError(
        "sector 0x%X did not erase within %.1fs (last status 0x%04X)"
        % (address, timeout, last))


def single_write(link, address, value, timeout=2.0):
    link.write_seq([(address, 0x70), (address, 0x10), (address, value)])
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = link.read_words(address, 1)[0] & 0xFFFF
        if last & 0x80:
            cart_reset(link, address)
            if (link.read_words(address, 1)[0] & 0xFFFF) == value:
                return True
            raise ProtocolError(
                "write 0x%04X to 0x%X did not verify"
                % (value, address))
        time.sleep(0.01)
    raise ProtocolError(
        "program of 0x%04X at 0x%X timed out (last status 0x%04X)"
        % (value, address, last))


def read_region(link, address, length):
    out = bytearray()
    for offset in range(0, length, BLOCK_BYTES):
        size = min(BLOCK_BYTES, length - offset)
        words = link.read_words(address + offset, (size + 3) // 4)
        out += b"".join(struct.pack("<I", word) for word in words)[:size]
    return bytes(out)


def write_block(link, address, data, timeout=10.0):
    if len(data) > BLOCK_BYTES or len(data) % 4:
        raise ValueError("block must be 4..%d bytes, word aligned"
                         % BLOCK_BYTES)
    words = struct.unpack("<%dH" % (len(data) // 2), data)
    packed = [words[index] | (words[index + 1] << 16)
              for index in range(0, len(words), 2)]
    frame = [(HDR_MAGIC << 24) | (OP_WRITE_BLOCK << 8),
             address & 0xFFFFFFFF, len(packed)] + packed
    link.xfer(frame, timeout=timeout)
    # The payload ignores the link while it programs, so wait for its OK.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status = link.ping(timeout=0.5)
        except ProtocolError:
            continue
        if (status & 0xFFFFFF00) == ST_OK:
            return
        time.sleep(0.01)
    raise ProtocolError("block write at 0x%X did not complete" % address)


def _sector_hashes(image):
    return [hashlib.sha256(image[offset:offset + SECTOR_BYTES]).hexdigest()
            for offset in range(0, len(image), SECTOR_BYTES)]


def _load_state(path):
    try:
        state = json.loads(Path(path).read_text())
    except Exception:
        return None
    if state.get("sector_bytes") != SECTOR_BYTES:
        return None
    return state


def _fingerprint(image):
    return hashlib.sha256(image[:FINGERPRINT_BYTES]).hexdigest()


def _save_state(path, image, hashes):
    Path(path).write_text(json.dumps({
        "image_sha256": hashlib.sha256(image).hexdigest(),
        "fingerprint": _fingerprint(image),
        "sector_bytes": SECTOR_BYTES,
        "size": len(image),
        "sectors": hashes,
    }, indent=1))


def _verify_sector(link, start, chunk, attempts=3):
    back = None
    for attempt in range(attempts):
        back = read_region(link, start, len(chunk))
        if back == chunk:
            return None
        if attempt + 1 < attempts:
            print(" retry ...", end="", flush=True)
            time.sleep(0.05)
    return back


def _program_sector(link, start, chunk):
    if start and start % RESET_EVERY == 0:
        cart_reset(link, start)
    sector_erase(link, start)
    written = 0
    for offset in range(0, len(chunk), BLOCK_BYTES):
        block = chunk[offset:offset + BLOCK_BYTES]
        if block == b"\xff" * len(block):
            continue
        write_block(link, start + offset, block)
        written += len(block)
    return written


def write_image(link, path, full=False, verify=True, boot=False,
                state_path=STATE_PATH):
    image = Path(path).read_bytes()
    if not image:
        raise ProtocolError("image is empty")
    if len(image) > 0x2000000:
        raise ProtocolError("image is larger than the 32 MiB cartridge")
    image += b"\xff" * (-len(image) % SECTOR_BYTES)
    hashes = _sector_hashes(image)
    print("image: %s (%d bytes, sha256 %s)"
          % (path, len(image), hashlib.sha256(image).hexdigest()))

    state = None if full else _load_state(state_path)
    if state is not None and (state.get("size") != len(image)
                              or len(state.get("sectors", [])) != len(hashes)):
        state = None
    if state is not None:
        # The state is only a hint: confirm the cartridge still looks like it
        # did when the state was written before skipping the full diff read.
        current = _fingerprint(read_region(link, 0, FINGERPRINT_BYTES))
        if current != state.get("fingerprint"):
            print("cartridge changed outside the tool; reading it back")
            state = None

    if full:
        dirty = list(range(len(hashes)))
        print("full: rewriting all %d sectors" % len(dirty))
    elif state is not None:
        dirty = [index for index, digest in enumerate(hashes)
                 if state["sectors"][index] != digest]
        print("state: %d of %d sectors changed" % (len(dirty), len(hashes)))
    else:
        print("reading %d bytes from the cartridge ..." % len(image))
        current = read_region(link, 0, len(image))
        dirty = [index for index in range(len(hashes))
                 if current[index * SECTOR_BYTES:(index + 1) * SECTOR_BYTES]
                 != image[index * SECTOR_BYTES:(index + 1) * SECTOR_BYTES]]
        print("diff: %d of %d sectors changed" % (len(dirty), len(hashes)))

    if not dirty:
        print("cartridge already matches the image")
        if boot:
            link.boot()
        return

    # A partial flash must not leave a state file that claims a match.
    Path(state_path).unlink(missing_ok=True)

    for number, index in enumerate(dirty, 1):
        start = index * SECTOR_BYTES
        chunk = image[start:start + SECTOR_BYTES]
        print("[%d/%d] sector 0x%X ..."
              % (number, len(dirty), start), end="", flush=True)
        back = None
        for attempt in range(2):
            written = _program_sector(link, start, chunk)
            print(" wrote %d bytes" % written, end="", flush=True)
            if not verify:
                break
            back = _verify_sector(link, start, chunk)
            if back is None:
                print(", verified", end="")
                break
            print(" mismatch, rewriting ...", end="", flush=True)
        if verify and back is not None:
            offset = next(position for position in range(len(chunk))
                          if back[position] != chunk[position])
            raise ProtocolError(
                "verify failed in sector 0x%X at 0x%X (expected %s, got %s)"
                % (start, offset,
                   chunk[offset:offset + 8].hex(" "),
                   back[offset:offset + 8].hex(" ")))
        print()

    _save_state(state_path, image, hashes)
    print("done")
    if boot:
        link.boot()



def swap_bits(value, pair):
    low, high = pair
    if ((value >> low) & 1) ^ ((value >> high) & 1):
        value ^= (1 << low) | (1 << high)
    return value


def parse_cfi(buffer):
    swaps = {
        b"QRY": [],
        b"RQZ": [(0, 1)],
        b"\x92\x91\x9a": [(0, 1), (6, 7)],
    }
    magic = buffer[0x20:0x21] + buffer[0x22:0x23] + buffer[0x24:0x25]
    if magic not in swaps:
        raise ProtocolError("no CFI signature at 0x20/0x22/0x24 (%r)" % magic)
    pins = swaps[magic]
    data = bytearray(buffer)
    for pair in pins:
        for index in range(len(data)):
            data[index] = swap_bits(data[index], pair)

    size = 1 << data[0x4E]
    vdd_min = (data[0x36] >> 4) + (data[0x36] & 0x0F) / 10.0
    vdd_max = (data[0x38] >> 4) + (data[0x38] & 0x0F) / 10.0
    regions = []
    position = 0
    for index in range(min(4, data[0x58])):
        count = (data[0x5C + index * 8] << 8 | data[0x5A + index * 8]) + 1
        block = (data[0x60 + index * 8] << 8 | data[0x5E + index * 8]) * 256
        regions.append((block, count))
        position += block * count
    return {
        "pins": pins,
        "size": size,
        "vdd_min": vdd_min,
        "vdd_max": vdd_max,
        "regions": regions,
        "data": data,
    }


# -- CLI ----------------------------------------------------------------------

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
    subprocess.run(command, check=True)


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
        import serial
        print("pyserial: ok (%s)" % serial.__version__)
    except ImportError:
        print("pyserial: MISSING; python3 -m pip install pyserial")
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
                   help="check the payload, pyserial, gbasend, and port")
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

    erase = sub.add_parser("erase", parents=[common],
                           help="erase one 128 KiB sector")
    erase.add_argument("address", type=lambda value: int(value, 0))
    erase.add_argument("--timeout", type=float, default=12.0)

    program = sub.add_parser("program", parents=[common],
                             help="program one 16-bit word")
    program.add_argument("address", type=lambda value: int(value, 0))
    program.add_argument("value", type=lambda value: int(value, 0))

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
    image.add_argument("--state", default=str(STATE_PATH))

    args = parser.parse_args(argv)
    # The shared parent actions use SUPPRESS so the subparser cannot overwrite
    # a value already given before the subcommand; fill the defaults here.
    defaults = {
        "port": None,
        "baud": 1_250_000,
        "upload_baud": 256_000,
        "gbasend": "gbasend",
        "payload": str(PAYLOAD),
        "handshake_wait": 60,
        "verbose": False,
    }
    for key, value in defaults.items():
        if not hasattr(args, key):
            setattr(args, key, value)

    if args.command == "doctor":
        return doctor(args)

    if args.command == "upload":
        args.port = find_port(args.port)
        upload(args)
        return 0

    args.port = find_port(args.port)
    link = open_link(args)
    try:
        if args.command == "id":
            print(cart_id(link).hex(" ").upper())
        elif args.command == "cfi":
            info = parse_cfi(cart_cfi(link))
            print("CFI signature: OK")
            print("swapped pins: %s" % (info["pins"] or "none"))
            print("size: 0x%X (%.2f MiB)"
                  % (info["size"], info["size"] / (1024 * 1024)))
            print("voltage: %.1f-%.1f V" % (info["vdd_min"], info["vdd_max"]))
            position = 0
            for block, count in info["regions"]:
                end = position + block * count
                print("region 0x%07X-0x%07X: 0x%X x %d"
                      % (position, end - 1, block, count))
                position = end
        elif args.command == "read":
            count = (args.length + 3) // 4
            words = link.read_words(args.address, count)
            data = b"".join(struct.pack("<I", word) for word in words)
            Path(args.output).write_bytes(data[:args.length])
            print("wrote %d bytes to %s" % (args.length, args.output))
        elif args.command == "erase":
            if sector_erase(link, args.address, args.timeout):
                print("sector 0x%X erased" % args.address)
        elif args.command == "program":
            single_write(link, args.address, args.value)
            print("0x%04X programmed at 0x%X" % (args.value, args.address))
        elif args.command == "seq":
            pairs = []
            for item in args.pairs:
                address, value = item.split(":")
                pairs.append((int(address, 0), int(value, 0)))
            link.write_seq(pairs)
            print("wrote %d pairs" % len(pairs))
        elif args.command == "image":
            write_image(link, args.file, full=args.full,
                        verify=not args.no_verify, boot=args.boot,
                        state_path=args.state)
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
