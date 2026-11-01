#!/usr/bin/env python3
"""Pad a GBA multiboot ROM and fix its cartridge header.

This replaces gbafix for the payload: it pads the image to a 16-byte multiple
with 0xFF, writes the title, game code, maker, and version, fills the Nintendo
logo that the BIOS requires, and stores the header complement check.
"""

import argparse
from pathlib import Path

# The 156-byte Nintendo logo at 0x04, required by the BIOS to boot a ROM.
LOGO = bytes.fromhex(
    "24ffae51699aa2213d84820a84e409ad11248b98c0817f21a352be199309ce20"
    "10464a4af82731ec58c7e83382e3cebf85f4df94ce4b09c194568ac01372a7fc"
    "9f844d73a3ca9a615897a327fc039876231dc7610304ae56bf38840040a70efd"
    "ff52fe036f9530f197fbc08560d68025a963be03014e38e2f9a234ffbb3e0344"
    "780090cb88113a9465c07c6387f03cafd625e48b380aac7221d4f807"
)


def fix(path, title, code, maker, version):
    data = bytearray(Path(path).read_bytes())
    if len(data) < 0xC0:
        raise SystemExit("%s is shorter than the cartridge header" % path)
    data += b"\xff" * (-len(data) % 16)

    data[0x04:0x04 + len(LOGO)] = LOGO
    data[0xA0:0xAC] = title.encode("ascii").ljust(12, b"\x00")[:12]
    data[0xAC:0xB0] = code.encode("ascii").ljust(4, b"\x00")[:4]
    data[0xB0:0xB2] = maker.encode("ascii").ljust(2, b"\x00")[:2]
    data[0xB2] = 0x96
    data[0xBC] = version & 0xFF

    checksum = 0x19 + sum(data[0xA0:0xBD])
    data[0xBD] = (-checksum) & 0xFF

    Path(path).write_bytes(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", help=".gba image to fix in place")
    parser.add_argument("-t", "--title", default="GBAFLASH")
    parser.add_argument("-c", "--code", default="GBAF")
    parser.add_argument("-m", "--maker", default="00")
    parser.add_argument("-r", "--version", type=lambda value: int(value, 0),
                        default=0)
    args = parser.parse_args()
    fix(args.file, args.title, args.code, args.maker, args.version)


if __name__ == "__main__":
    main()
