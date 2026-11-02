#!/usr/bin/env python3
"""Build the FlashGBLink plugin release zip.

The archive extracts directly into FlashGBX's ``plugins`` directory:

    flashgblink_plugin.py
    flashgblink/
    INSTALL.txt
"""

import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "dist" / "FlashGBLink-plugin.zip"
FILES = ("flashgblink_plugin.py",)
DIRS = ("flashgblink",)
INSTALL = """FlashGBLink plugin
===================

Extract this archive into FlashGBX's plugins directory:

  macOS app:
    /Applications/FlashGBX.app/Contents/MacOS/config/plugins

  Linux:
    ~/.config/FlashGBX/plugins

  Windows:
    %LOCALAPPDATA%\\FlashGBX\\plugins

The result should be:

  plugins/flashgblink_plugin.py
  plugins/flashgblink/

Start FlashGBX, approve FlashGBLink when prompted, then connect to it.
Hold START+SELECT while powering the GBA on; the Connect dialog can upload the
payload automatically using gbasend.  The project README documents the
manual/headless fallback.
"""


def main():
    OUT.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in FILES:
            archive.write(ROOT / name, name)
        for directory in DIRS:
            for path in sorted((ROOT / directory).rglob("*")):
                if path.is_dir():
                    continue
                if "__pycache__" in path.parts or path.suffix == ".pyc":
                    continue
                archive.write(path, str(path.relative_to(ROOT)))
        payload = ROOT / "payload" / "flasher.gba"
        if payload.is_file():
            archive.write(payload, "flashgblink/payload/flasher.gba")
        else:
            print("warning: payload/flasher.gba is missing")
        archive.writestr("INSTALL.txt", INSTALL)
    print(OUT)
    print("%d bytes" % OUT.stat().st_size)


if __name__ == "__main__":
    main()
