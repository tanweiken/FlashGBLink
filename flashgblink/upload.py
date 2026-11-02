"""Find the payload and gbasend, and build the multiboot upload command."""

import os
import shutil
from pathlib import Path

from .gbalink import Link, ST_OK, find_port

DEFAULT_UPLOAD_BAUD = 256_000
DEFAULT_HANDSHAKE_WAIT = 30


def _config_dir():
    try:
        from FlashGBX.app import AppContext
        return Path(AppContext.CONFIG_PATH) if AppContext.CONFIG_PATH else None
    except Exception:
        return None


def _resolve_path(value):
    """Resolve an absolute path or a path relative to the plugin/repo/config."""
    path = Path(value).expanduser()
    if path.is_absolute():
        return path if path.is_file() else None
    bases = [
        Path(__file__).resolve().parent,          # flashgblink/
        Path(__file__).resolve().parents[1],      # plugin dir / repo root
        _config_dir(),
        Path.cwd(),
    ]
    for base in bases:
        if base is None:
            continue
        candidate = base / path
        if candidate.is_file():
            return candidate
    return None


def _setting(key):
    """Read an optional FlashGBX settings.ini key."""
    try:
        from FlashGBX.app import AppContext
        from FlashGBX.IniSettings import IniSettings
        if not AppContext.CONFIG_PATH:
            return None
        settings = IniSettings(
            path=str(Path(AppContext.CONFIG_PATH) / "settings.ini"))
        value = settings.value(key)
        return value.strip() if isinstance(value, str) and value.strip() else None
    except Exception:
        return None


def find_payload():
    """Return the payload path, or None if it has not been built."""
    env = os.environ.get("FLASHGBLINK_PAYLOAD") or _setting("FlashGBLinkPayload")
    candidates = []
    if env:
        resolved = _resolve_path(env)
        if resolved is not None:
            candidates.append(resolved)
    candidates.append(Path(__file__).resolve().parent / "payload" / "flasher.gba")
    candidates.append(Path(__file__).resolve().parents[1] / "payload" / "flasher.gba")
    for path in candidates:
        if path.is_file():
            return path
    return None


def find_gbasend():
    """Return the gbasend executable, or None if it cannot be found."""
    env = os.environ.get("FLASHGBLINK_GBASEND") or _setting("FlashGBLinkGbasend")
    if env:
        path = _resolve_path(env)
        if path is not None:
            return path
    found = shutil.which("gbasend")
    if found:
        return Path(found)
    for name in ("bin/gbasend", "bin/gbasend.exe"):
        candidate = _resolve_path(name)
        if candidate is not None:
            return candidate
    for candidate in (
        Path.home() / "gbasend-rs/gbasend-cli/target/release/gbasend",
        Path.home() / ".cargo/bin/gbasend",
    ):
        if candidate.is_file():
            return candidate
    return None


def find_link_port():
    """Return the Pico serial port, or None."""
    try:
        return find_port()
    except (SystemExit, Exception):
        return None


def payload_running(port):
    """True if the GBA payload answers on this port."""
    if not port:
        return False
    try:
        link = Link(port, 1_250_000)
    except Exception:
        return False
    try:
        return (link.ping(timeout=0.5) & 0xFFFF0000) == ST_OK
    except Exception:
        return False
    finally:
        link.close()


def upload_command(gbasend, port, payload, baud=DEFAULT_UPLOAD_BAUD,
                   handshake_wait=DEFAULT_HANDSHAKE_WAIT):
    return [
        str(gbasend),
        "-p", str(port),
        "-b", str(baud),
        "-m",
        "--handshake-wait", str(handshake_wait),
        str(payload),
    ]
