#!/usr/bin/env python3
"""Install the FlashGBLink plugin into FlashGBX.

Copies exactly what FlashGBX needs:

    <plugins>/flashgblink_plugin.py
    <plugins>/flashgblink/
    <plugins>/gbalink/

The script finds the FlashGBX configuration directory automatically.  Pass
``--plugins-dir`` or set ``FLASHGBLINK_PLUGIN_DIR`` if it guesses wrong.
"""

import argparse
import os
import platform
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_FILE = ROOT / "flashgblink_plugin.py"
PLUGIN_DIRS = ("flashgblink", "gbalink")


def candidates():
    env = os.environ.get("FLASHGBLINK_PLUGIN_DIR")
    if env:
        yield Path(env).expanduser()
    system = platform.system()
    if system == "Darwin":
        yield Path("/Applications/FlashGBX.app/Contents/MacOS/config/plugins")
        yield Path.home() / "Library/Preferences/FlashGBX/plugins"
        yield Path.home() / "Library/Application Support/FlashGBX/plugins"
    elif system == "Windows":
        for variable in ("LOCALAPPDATA", "APPDATA"):
            base = os.environ.get(variable)
            if base:
                yield Path(base) / "FlashGBX/plugins"
    else:
        yield Path.home() / ".config/FlashGBX/plugins"
        yield Path.home() / ".config/flashgbx/plugins"


def find_plugin_dir(explicit=None):
    if explicit:
        return Path(explicit).expanduser()
    seen = []
    for path in candidates():
        if path in seen:
            continue
        seen.append(path)
        if path.is_dir():
            return path
        # FlashGBX may not have created plugins/ yet.
        if path.parent.is_dir() and (path.parent / "settings.ini").is_file():
            return path
    listing = "\n".join("  %s" % path for path in seen)
    raise SystemExit(
        "could not find the FlashGBX plugins directory.\n"
        "Tried:\n%s\n"
        "Pass --plugins-dir or set FLASHGBLINK_PLUGIN_DIR." % listing)


def install(target, dry_run=False):
    target = Path(target)
    print("FlashGBX plugins: %s" % target)
    if dry_run:
        print("would copy %s" % PLUGIN_FILE.name)
        for name in PLUGIN_DIRS:
            print("would copy %s/" % name)
        return
    try:
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PLUGIN_FILE, target / PLUGIN_FILE.name)
        for name in PLUGIN_DIRS:
            destination = target / name
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(
                ROOT / name, destination,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    except PermissionError:
        raise SystemExit(
            "permission denied writing to %s; run with sudo or pass "
            "--plugins-dir" % target)
    print("Installed FlashGBLink. Restart FlashGBX, then approve the plugin "
          "and connect to FlashGBLink.")


def uninstall(target):
    target = Path(target)
    for path in [target / PLUGIN_FILE.name] + [target / name
                                               for name in PLUGIN_DIRS]:
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
    print("Removed FlashGBLink from %s" % target)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugins-dir",
                        help="FlashGBX plugins directory (auto-detected)")
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would be copied")
    parser.add_argument("--uninstall", action="store_true",
                        help="remove FlashGBLink from the plugins directory")
    args = parser.parse_args(argv)
    target = find_plugin_dir(args.plugins_dir)
    if args.uninstall:
        uninstall(target)
    else:
        install(target, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
