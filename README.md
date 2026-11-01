# GBA link-port cartridge flasher

Read and write a Game Boy Advance cartridge while it stays in the console,
using a Raspberry Pi Pico on the link port. No cartridge reader and no moving
the cartridge between a reader and the console.

```
PC (Python) --USB serial--> Pico (gbasend-pico) --SPI--> GBA payload --> cartridge
```

The Pico uploads a small payload into the GBA with the BIOS multiboot protocol.
The payload performs 16-bit cartridge stores and loads; the host composes every
flash command sequence, so supporting another cartridge does not require
rebuilding the payload.

## Status

Works on the bootleg 32 MiB `M36L0R8060B` cartridge (FlashGBX profile
`2006-36-71_V2 with M36L0R8060B`): ID and CFI read, sector erase, single-word
program, 64-byte buffered block writes, full-image flashing with delta and
verification, dump/read, and boot.

Not implemented yet: other cartridge profiles, save backup/restore.

## Hardware

- GBA or GBA SP with the cartridge inserted.
- RP2040 Pico. The firmware does not run on RP2350 or ESP32.
- Pico to link port: GP0 = GBA SO, GP2 = GBA SC, GP3 = GBA SI, GND, with the
  1 kOhm series resistor described in the gbasend-rs `DIY.md`.
- Power the Pico from USB only. Do not connect USB and the link port's VCC at
  the same time.

## Firmware

Flash `firmware/gbasend-pico.uf2` once: hold BOOTSEL, plug the Pico into USB,
copy the UF2 to the mounted `RPI-RP2` drive. It enumerates as a USB serial
device with VID:PID `2E8A:000A`, which `flash.py` autodetects. See
[`firmware/README.md`](firmware/README.md) for provenance and checksums.

## Build the payload

Requires only `arm-none-eabi-gcc` and Python 3 (no devkitPro):

```sh
make -C payload
```

Output: `payload/flasher.gba` (about 1.2 KiB). `fix_header.py` pads the image
and fills the cartridge header (logo and complement check).

## Host setup

```sh
python3 -m venv .venv
.venv/bin/pip install pyserial

git clone https://github.com/nullstalgia/gbasend-rs
cargo build --release --manifest-path gbasend-rs/gbasend-cli/Cargo.toml
GBASEND=$PWD/gbasend-rs/gbasend-cli/target/release/gbasend
```

Check the setup:

```sh
.venv/bin/python3 flash.py doctor --gbasend $GBASEND
```

## Usage

Insert the cartridge, then power the GBA on holding **START+SELECT** so the
BIOS waits for the multiboot upload. Start the upload, then power on:

```sh
PY=.venv/bin/python3

$PY flash.py --gbasend $GBASEND upload       # payload, ~2 s
$PY flash.py id                              # 20 00 0E 88 ...
$PY flash.py cfi                             # 32 MiB, 1.7-2.0 V, RQZ swap
```

The payload stays resident, so the following need no further button presses:

```sh
$PY flash.py read 0 0x100 rom-head.bin
$PY flash.py erase 0x1F00000
$PY flash.py program 0x1F00000 0xBEEF
$PY flash.py seq 0:0x50 0:0xFF
$PY flash.py image Apotris.gba --boot
$PY flash.py boot
```

`image` writes a `.gba` file, erasing only changed 128 KiB sectors:

```sh
# First time, or after erasing the cartridge with another tool:
$PY flash.py image Apotris.gba --full --boot

# Later runs: only changed sectors are touched.
$PY flash.py image Apotris.gba --boot
```

- The image is padded to 128 KiB with `0xFF`; matching sectors are skipped.
- `.flash-state.json` records per-sector hashes and a 4 KiB cartridge
  fingerprint, so later runs skip the 4 MiB diff read. If the fingerprint does
  not match, the cartridge is read and diffed. `--full` rewrites everything
  without reading; `--no-verify` skips the read-back.
- Each changed sector is erased, programmed in 4 KiB blocks with 64-byte
  buffered writes, and read back when verifying.

Options: `--port`, `--baud` (data phase SPI clock, default 1.25 MHz),
`--upload-baud` (multiboot clock, default 256 kHz), `--gbasend`, `--payload`,
`--verbose`.

## Measured timings

On the 32 MiB M36L0R8060B cartridge with a 4 MiB image:

| Operation | Time |
|---|---|
| full rewrite, 32 sectors, verify | 6:03 |
| one 128 KiB sector (erase, write, verify) | ~11 s |
| full read (4 MiB) | ~2.3 min |

Throughput is about 7,615 words/s (~30 KB/s). The Pico firmware pauses 100 µs
after every 4-byte word, so the clock (2.0 MHz maximum) is not the limit.

## Word protocol

All traffic is 32-bit words, MSB-first. The host sends a word and receives one
back; the payload preloads its reply, so a reply to a command word is the
previous status.

| Word | Meaning |
|---|---|
| `0x5A5A5A5A` | ping; reply is the current status |
| `0x5A20nn` | write `nn` address/value pairs (two words per pair) |
| `0x5A3000` | read: next words are address and count, then `count` dummies |
| `0x5A5000` | write block: next words are address and packed word count, then data |
| `0x5A4000` | soft reset into the cartridge |
| `0xA5A50000` | OK |
| `0xA5A500E1` | rejected command |

## Cartridge notes

The M36 tracks Intel command state per 8 MiB bank. The reset (`0x50`, `0xFF`)
must be issued at an address inside the bank being erased or programmed,
otherwise that bank stays in status-register mode and reads return `0x0080`.

The buffered-write count field is **bytes minus one** (63 for 64 bytes).

The BIOS soft reset only boots the cartridge when the flag at `0x03007FFA` is
zero; multiboot leaves it non-zero.

## Roadmap

- Save backup/restore (64 KiB SRAM here, other save types later).
- FlashGBX profile loading and automatic cartridge detection: read the ID and
  CFI, match against `config/fc_AGB_*.txt`, then use that profile's sequences.
- Faster link: remove the Pico's fixed 100 µs per-word delay and replace it with
  flow control (block acknowledgements or a ready line).
- Optional web UI for auto-updating a ROM over the link.

## License

GPL-3.0-or-later. See `LICENSE` and `NOTICE`.
