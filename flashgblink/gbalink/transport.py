"""Word exchange with the gbasend-pico bridge and the GBA payload.

The host sends 32-bit words MSB-first and receives one word back per word
sent.  The payload preloads its reply, so a reply to a command word is the
previous status.  Everything above this layer works in cartridge byte
addresses; the payload's 16-bit stores and loads are what make that possible.
"""

import struct
import threading
import time
from pathlib import Path

from .errors import ProtocolError, TransferError

GBASEND_VID = 0x2E8A
GBASEND_PID = 0x000A

PING = 0x5A5A5A5A
HDR_MAGIC = 0x5A

OP_WRITE_SEQ = 0x20
OP_READ = 0x30
OP_BOOT = 0x40
OP_WRITE_BLOCK = 0x50
OP_PROGRESS = 0x60
OP_READ8 = 0x70
OP_WRITE8 = 0x80
OP_SET_PLAN = 0x90

ST_OK = 0xA5A50000
ST_BAD = 0xA5A500E1

# A desynced transfer is recovered by clocking dummy words at the payload:
# they are either the remaining replies of the aborted command or invalid
# command headers, which it rejects. The count covers the largest frame the
# payload can still be holding (a full 4 KiB block write, then some).
RESYNC_WORDS = 1100

PACKAGE_DIR = Path(__file__).resolve().parent.parent
PAYLOAD = PACKAGE_DIR / "payload" / "flasher.gba"
if not PAYLOAD.is_file():
    PAYLOAD = PACKAGE_DIR.parent / "payload" / "flasher.gba"
BLOCK_BYTES = 4096


def _require_serial():
    try:
        import serial
        from serial.tools import list_ports
    except ImportError:  # pragma: no cover - environment dependent
        raise SystemExit(
            "pyserial is required: python3 -m pip install pyserial")
    return serial, list_ports


def find_port(explicit=None):
    if explicit:
        return explicit
    _, list_ports = _require_serial()
    ports = list(list_ports.comports())
    for info in ports:
        if info.vid == GBASEND_VID and info.pid == GBASEND_PID:
            return info.device
    # A Pico that enumerates under the Raspberry Pi VID but with another PID is
    # usually running MicroPython or a stock Pico program, not gbasend-pico.
    other = [info for info in ports if info.vid == GBASEND_VID]
    if other:
        info = other[0]
        raise SystemExit(
            "found Raspberry Pi USB serial %s (VID:PID %04X:%04X, %s) but it "
            "is not gbasend-pico; flash firmware/gbasend-pico.uf2 and "
            "reconnect" % (info.device, info.vid, info.pid,
                           info.product or info.description))
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
        # Bytes left in the OS buffer by an earlier aborted transfer would
        # offset every reply, so start from a quiet line.
        self.ser.reset_input_buffer()
        self._buffer = bytearray()
        self._cond = threading.Condition()
        self._lock = threading.RLock()
        self._closed = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self):
        while not self._closed:
            try:
                # Read what is available instead of waiting for a full 4 KiB.
                # FlashGBX changes the serial timeout, and a blocking 4 KiB
                # read would delay every small reply until that timeout.
                data = self.ser.read(max(1, self.ser.in_waiting))
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
        if (self._reader.is_alive()
                and self._reader is not threading.current_thread()):
            self._reader.join(timeout=0.5)

    def xfer(self, words, timeout=5.0):
        # FlashGBX can check device liveness from another thread while a
        # transfer is in progress; a second writer would corrupt the framing.
        with self._lock:
            return self._xfer(words, timeout)

    def _xfer(self, words, timeout=5.0):
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
                    got = len(self._buffer) - len(self._buffer) % 4
                    raise TransferError(
                        "timeout waiting for %d reply bytes (got %d)"
                        % (expected, got),
                        missing=len(words) - got // 4)
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

    def drain(self):
        with self._lock:
            with self._cond:
                self._buffer.clear()

    def resync(self, attempts=3):
        """Re-establish word framing after a short transfer.

        The payload may still be finishing the command whose reply was cut
        short, so it is fed dummy words: they are either its remaining replies
        or invalid command headers, which it rejects with ST_BAD.
        """
        with self._lock:
            self.drain()
            self.ser.write(_pack_words([0] * RESYNC_WORDS))
            self.ser.flush()
            time.sleep(0.05)
            self.drain()
            for _ in range(attempts):
                try:
                    word = self.ping(timeout=0.5)
                except ProtocolError:
                    word = None
                if word is not None and (word & 0xFFFF0000) == ST_OK:
                    return True
                self.drain()
            return False

    @staticmethod
    def _unrecovered():
        return ProtocolError(
            "link did not recover; replug the Pico's USB cable, power the GBA "
            "off and on holding START+SELECT, then upload the payload again")

    def xfer_with_recovery(self, words, timeout=5.0):
        try:
            return self.xfer(words, timeout=timeout)
        except TransferError:
            if not self.resync():
                raise self._unrecovered()
            return self.xfer(words, timeout=timeout)

    def handshake(self, timeout=8.0):
        """Wait for the payload to answer after a multiboot upload."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                word = self.ping(timeout=0.5)
            except ProtocolError:
                self.drain()
                time.sleep(0.05)
                continue
            if (word & 0xFFFF0000) == 0xA5A50000:
                return word
            self.drain()
            time.sleep(0.05)
        raise ProtocolError(
            "no payload response; upload the payload with FlashGBLink/Connect "
            "or `flash.py upload` first")

    def write_seq(self, pairs, timeout=5.0):
        for start in range(0, len(pairs), 200):
            chunk = pairs[start:start + 200]
            words = [(HDR_MAGIC << 24) | (OP_WRITE_SEQ << 8) | len(chunk)]
            for address, value in chunk:
                words.append(address & 0xFFFFFFFF)
                words.append(value & 0xFFFF)
            words.append(PING)
            status = self.xfer_with_recovery(words, timeout=timeout)[-1]
            _check_status(status)

    def set_write_plan(self, chunk_bytes, commands, timeout=2.0):
        """Install a profile's write template in the payload.

        ``commands`` are already encoded plan words (four per command).  The
        payload keeps them until the next block write, so a 4 KiB block is
        split into ``chunk_bytes`` pieces without another round trip.
        """
        words = [(HDR_MAGIC << 24) | (OP_SET_PLAN << 8),
                 chunk_bytes & 0xFFFFFFFF, len(commands) // 4]
        words.extend(commands)
        words.append(PING)
        status = self.xfer_with_recovery(words, timeout=timeout)[-1]
        _check_status(status)

    def read_words(self, address, count, timeout=10.0):
        words = [(HDR_MAGIC << 24) | (OP_READ << 8), address & 0xFFFFFFFF,
                 count & 0xFFFFFFFF]
        words += [0] * count
        words.append(PING)
        replies = self.xfer_with_recovery(words, timeout=timeout)
        _check_status(replies[-1])
        return replies[3:3 + count]

    def read8(self, address, count, timeout=10.0):
        words = (count + 3) // 4
        frame = [(HDR_MAGIC << 24) | (OP_READ8 << 8), address & 0xFFFFFFFF,
                 count & 0xFFFFFFFF] + [0] * words + [PING]
        replies = self.xfer_with_recovery(frame, timeout=timeout)
        _check_status(replies[-1])
        raw = b"".join(struct.pack("<I", word) for word in replies[3:3 + words])
        return bytes(raw[:count])

    def write8(self, address, data, timeout=10.0):
        if len(data) > BLOCK_BYTES:
            raise ValueError("byte write is limited to %d bytes" % BLOCK_BYTES)
        packed = [int.from_bytes(data[index:index + 4], "little")
                  for index in range(0, len(data), 4)]
        frame = [(HDR_MAGIC << 24) | (OP_WRITE8 << 8), address & 0xFFFFFFFF,
                 len(data)] + packed
        self.xfer_with_recovery(frame, timeout=timeout)
        # The payload stores the bytes after the last word has arrived, so its
        # status is fetched by pinging, the same way a block write is.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                status = self.ping(timeout=0.05)
            except ProtocolError:
                continue
            if (status & 0xFFFFFF00) == ST_OK:
                return
        raise ProtocolError("byte write at 0x%X did not complete" % address)

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


def read_region(link, address, length):
    out = bytearray()
    for offset in range(0, length, BLOCK_BYTES):
        size = min(BLOCK_BYTES, length - offset)
        words = link.read_words(address + offset, (size + 3) // 4)
        out += b"".join(struct.pack("<I", word) for word in words)[:size]
    return bytes(out)


def progress(link, percent, phase=0, timeout=2.0):
    """Draw a progress bar on the GBA's screen.

    ``phase`` selects the write bar (0, green) or the verify bar (1, cyan).
    Drawing outlasts the bridge's inter-word delay, so the payload cannot
    answer the trailing word in step; the update is confirmed by pinging
    instead, the same way a block write is.
    """
    percent = max(0, min(100, int(percent)))
    header = ((HDR_MAGIC << 24) | ((int(phase) & 0xFF) << 16)
              | (OP_PROGRESS << 8) | percent)
    link.xfer_with_recovery([header, PING])
    # The payload spends a couple of milliseconds drawing before it preloads
    # its reply; pinging into that window would burn a full timeout.
    time.sleep(0.01)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status = link.ping(timeout=0.5)
        except ProtocolError:
            continue
        if (status & 0xFFFFFF00) == ST_OK:
            return
    raise ProtocolError("progress update %d%% was not acknowledged" % percent)


def write_block(link, address, data, timeout=30.0):
    if len(data) > BLOCK_BYTES or len(data) % 4:
        raise ValueError("block must be 4..%d bytes, word aligned"
                         % BLOCK_BYTES)
    words = struct.unpack("<%dH" % (len(data) // 2), data)
    packed = [words[index] | (words[index + 1] << 16)
              for index in range(0, len(words), 2)]
    frame = [(HDR_MAGIC << 24) | (OP_WRITE_BLOCK << 8),
             address & 0xFFFFFFFF, len(packed)] + packed
    link.xfer_with_recovery(frame, timeout=timeout)
    # The payload ignores the link while it programs, so wait for its OK. A
    # ping clocked into that window is lost, so retry with a short timeout
    # rather than paying half a second each time.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status = link.ping(timeout=0.05)
        except ProtocolError:
            continue
        if status == ST_BAD:
            raise ProtocolError("payload rejected block write at 0x%X"
                                % address)
        if (status & 0xFFFFFF00) == ST_OK:
            return
        time.sleep(0.01)
    raise ProtocolError("block write at 0x%X did not complete" % address)
