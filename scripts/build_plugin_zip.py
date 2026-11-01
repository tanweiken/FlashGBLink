#!/usr/bin/env python3
"""Build the FlashGBLink plugin release zip.

The archive extracts directly into FlashGBX's ``plugins`` directory:

    flashgblink_plugin.py
    flashgblink/
    gbalink/
    INSTALL.txt
"""

import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "dist" / "FlashGBLink-plugin.zip"
FILES = ("flashgblink_plugin.py",)
DIRS = ("flashgblink", "gbalink")
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
  plugins/gbalink/

Start FlashGBX, approve FlashGBLink when prompted, then connect to it.
The GBA must already be running the flasher payload; see the project README.
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
        archive.writestr("INSTALL.txt", INSTALL)
    print(OUT)
    print("%d bytes" % OUT.stat().st_size)


if __name__ == "__main__":
    main()
