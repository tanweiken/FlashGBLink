# FlashGBLink

Read and write a Game Boy Advance cartridge while it stays in the console,
using a Raspberry Pi Pico on the link port. **FlashGBLink** presents the
adapter to [FlashGBX](https://github.com/Lesserkuma/FlashGBX) as a normal
cartridge-reader device, so FlashGBX provides the desktop interface, profile
database, detection, ROM/save backup and verified flashing. A standalone
`flash.py` CLI covers scripted and headless use.

```
PC (FlashGBX + FlashGBLink plugin) --USB serial--> Pico (gbasend-pico)
    --SPI--> GBA payload --> cartridge
```

No cartridge reader is needed, and the cartridge never has to move between a
reader and the console. The Pico uploads a small payload into the GBA with the
BIOS multiboot protocol. The payload performs 16-bit cartridge stores and
loads and executes the write template the host installs from the detected
FlashGBX profile, so supporting another cartridge does not require rebuilding
the payload.

## Repository layout

```
flashgblink/            FlashGBX device plugin (the primary use case)
flashgblink_plugin.py   plugin entry point for FlashGBX's plugins directory
gbalink/                host-side link transport and write-plan encoding
flash.py                standalone CLI (alternative, uses FlashGBX's profiles)
flashgbx.py             CLI profile loader and FlashGBX CFI adapter
payload/                GBA-side flasher source
firmware/               prebuilt gbasend-pico UF2s
tests/                  hardware-free tests and small profile fixtures
docs/                   hardware baseline notes
```

## Status

Works on the bootleg 32 MiB `M36L0R8060B` cartridge (FlashGBX profile
`2006-36-71_V2 with M36L0R8060B`): ID and CFI read, sector erase, single-word
program, buffered block writes, full-image flashing with verification,
dump/read, SRAM save backup/restore, and boot.

The cartridge is detected automatically: the FlashGBX `Flashcart` class probes
the identification commands, matches the flash ID and decodes CFI. The payload
then executes the selected profile's sector map, reset and write sequences.
Intel and AMD command sets are supported, including per-profile buffer sizes
from 32 bytes to 4 KiB. The M36 path is the one tested on hardware; other
profiles are loaded and detected but have not been exercised on real
cartridges.

Measured on that cartridge with a 4 MiB Apotris image:

| Operation | Result |
|---|---|
| FlashGBLink full write through FlashGBX, verified | 6:44–6:48 |
| 4 MiB read through FlashGBLink | ~2:40 |
| CLI full rewrite, 32 sectors, verify | 6:03 (6:07 with the progress bar) |
| Full read, 4 MiB | ~2.3 min |
| SRAM dump / verified restore (64 KiB) | 2.6 s / 6.8 s |

The GBA draws two progress bars while FlashGBLink writes: **green** for the
write phase and **cyan** for verification. A short or stalled transfer is
recovered in place — buffer drained, framing re-synced, command retried —
instead of wedging the link.

Not implemented yet: EEPROM and save-flash writes, and cartridges that need
flash bank switching (over 32 MiB).

## Quick start

### 1. Hardware

- GBA or GBA SP with the cartridge inserted.
- RP2040 Pico. The firmware does not run on RP2350 or ESP32.
- Pico to link port: GP0 = GBA SO, GP2 = GBA SC, GP3 = GBA SI, GND, with the
  1 kOhm series resistor described in the gbasend-rs `DIY.md`.
- Power the Pico from USB only. Do not connect USB and the link port's VCC at
  the same time.

### 2. Flash the Pico firmware

Flash `firmware/gbasend-pico.uf2` once: hold BOOTSEL, plug the Pico into USB,
copy the UF2 to the mounted `RPI-RP2` drive. It enumerates as a USB serial
device with VID:PID `2E8A:000A`. See
[`firmware/README.md`](firmware/README.md) for provenance and checksums.

### 3. Build the payload

Requires only `arm-none-eabi-gcc` and Python 3 (no devkitPro):

```sh
make -C payload
```

Output: `payload/flasher.gba` (about 3.2 KiB). `fix_header.py` pads the image
and fills the cartridge header (logo and complement check). The payload holds
the profile's write template, so a new cartridge profile does not need a
payload rebuild.

### 4. Host setup

```sh
python3 -m venv .venv
.venv/bin/pip install pyserial FlashGBX

git clone https://github.com/nullstalgia/gbasend-rs
cargo build --release --manifest-path gbasend-rs/gbasend-cli/Cargo.toml
GBASEND=$PWD/gbasend-rs/gbasend-cli/target/release/gbasend
```

FlashGBLink needs FlashGBX **5.2.1 or newer** for its plugin system. If PyPI
still installs an older release, install FlashGBX from its `master` branch.
The GUI's Qt dependencies are not needed for `flash.py`'s use of the library.

Check the setup:

```sh
.venv/bin/python3 flash.py doctor --gbasend $GBASEND
```

### 5. Upload the payload

Insert the cartridge, then power the GBA on holding **START+SELECT** so the
BIOS waits for the multiboot upload, and run:

```sh
.venv/bin/python3 flash.py upload --gbasend $GBASEND
```

The payload stays resident until the GBA is reset or powered off.

### 6. Install the FlashGBLink plugin

FlashGBX loads plugins from the `plugins` subdirectory of its configuration
directory. The "appdata" location is shown by `python3 -m FlashGBX --help`
(`--cfgdir`); typical defaults are `~/.config/FlashGBX` on Linux,
`~/Library/Preferences/FlashGBX` on macOS and `%LOCALAPPDATA%\FlashGBX` on
Windows. The macOS release app keeps its configuration at
`/Applications/FlashGBX.app/Contents/MacOS/config`, so its plugin directory is
`/Applications/FlashGBX.app/Contents/MacOS/config/plugins`.

Copy the plugin and its two packages into that directory:

```sh
PLUGINS="/Applications/FlashGBX.app/Contents/MacOS/config/plugins"  # macOS app
# PLUGINS="$HOME/.config/FlashGBX/plugins"                          # Linux
mkdir -p "$PLUGINS"
cp flashgblink_plugin.py "$PLUGINS/"
cp -R flashgblink gbalink "$PLUGINS/"
```

Alternatively, copy only `flashgblink_plugin.py` and point it at this checkout:

```sh
export FLASHGBLINK_ROOT=/path/to/FlashGBLink
```

Start FlashGBX, approve **FlashGBLink** when prompted, then Connect →
FlashGBLink. The device only appears while the payload is running; it does not
upload during FlashGBX's device discovery, because the GBA must already be
waiting in multiboot.

### 7. Flash or back up

In FlashGBX use the normal **Detect Cartridge**, **Backup ROM**, **Write ROM**,
**Backup Save** and **Restore Save** actions. Detection selects the profile,
writes are verified, and the GBA shows the green write bar followed by the cyan
verify bar.

## CLI

The CLI uses the same payload and does not need the plugin, but it does use
FlashGBX's installed profile database and `Flashcart`/`CFI` classes:

```sh
PY=.venv/bin/python3

$PY flash.py upload --gbasend $GBASEND       # payload, ~2 s
$PY flash.py detect                          # ID, profile, CFI, sectors
$PY flash.py id                              # 20 00 0E 88 ...
$PY flash.py cfi                             # 32 MiB, 1.7-2.0 V, RQZ swap
$PY flash.py profiles                        # list FlashGBX's profiles
```

To skip autodetection (or to use a cartridge with no matching ID), pass any name
from a profile:

```sh
$PY flash.py image Apotris.gba --profile "2006-36-71_V2 with M36L0R8060B"
$PY flash.py image Apotris.gba --config /path/to/FlashGBX/config
```

The payload stays resident, so these need no further button presses:

```sh
$PY flash.py read 0 0x100 rom-head.bin
$PY flash.py erase 0x1F00000
$PY flash.py program 0x1F00000 0xBEEF
$PY flash.py seq 0:0x50 0:0xFF
$PY flash.py image Apotris.gba --boot
$PY flash.py save apotris.sav
$PY flash.py restore apotris.sav
$PY flash.py boot
```

`image` writes a `.gba` file, erasing only changed sectors from the detected
profile's sector map (128 KiB sectors on the M36):

```sh
# First time, or after erasing the cartridge with another tool:
$PY flash.py image Apotris.gba --full --boot

# Later runs: only changed sectors are touched.
$PY flash.py image Apotris.gba --boot
```

- The image is padded with `0xFF` to the end of its last sector; matching
  sectors are skipped.
- `.flash-state.json` records the sector map, per-sector hashes and a 4 KiB
  cartridge fingerprint, so later runs skip the 4 MiB diff read. If the
  fingerprint does not match, the cartridge is read and diffed. `--full`
  rewrites everything without reading; `--no-verify` skips the read-back.
- Each changed sector is erased, programmed in 4 KiB blocks using the profile's
  buffered-write template, and read back when verifying.

Options: `--port`, `--baud` (data phase SPI clock, default 1.25 MHz),
`--upload-baud` (multiboot clock, default 256 kHz), `--config` (profile
directory; default: autodetect FlashGBX's), `--profile` (profile name,
skips autodetection), `FLASHGBLINK_CONFIG` (profile directory override),
`--gbasend`, `--payload`, `--verbose`.

## Saves

`save` dumps the save media at `0x0E000000` to a raw `.sav` that emulators can
open; `restore` writes one back and reads it to confirm. Both take
`--size BYTES` for a different save media size (default `0x10000`, 64 KiB).

```sh
$PY flash.py save apotris.sav
$PY flash.py restore apotris.sav            # verified
$PY flash.py restore old.sav --no-verify
```

The save bus is 8 bits wide, so the payload has byte-wide reads and writes
(`0x5A70`/`0x5A80`) separate from the 16-bit cartridge path. A 16-bit load from
that region returns the same byte in both halves of the lane, which silently
doubles every byte and produces a `.sav` no emulator can read.

Back up before flashing: `image --full` erases the whole cartridge image area,
and a save that was never dumped is gone.

## How it works

- **Pico firmware** (`gbasend-pico`) bridges USB serial to the GBA link port.
- **Payload** (`payload/`) runs in GBA RAM after multiboot, performs 16-bit
  cartridge stores/loads, byte-wide save access, the progress bars and a soft
  reset, and holds the profile's write template.
- **FlashGBLink plugin** (`flashgblink/`) implements the AGB-relevant subset of
  FlashGBX's device protocol on top of `gbalink/`, so FlashGBX's `LK_Device`,
  `Flashcart` and profile database run unchanged.
- **CLI** (`flash.py`) uses the same `gbalink/` transport and FlashGBX's
  `Flashcart`/CFI for detection, plus a delta image writer.

## Limitations

- AGB cartridges only.
- EEPROM and save-flash writes are not implemented.
- Profiles that require a cartridge power cycle abort with a prompt; power the
  GBA off and on holding START+SELECT, re-upload the payload and reconnect.
- FlashGBX erases every sector in the image; the `.flash-state.json` delta fast
  path is only available from `flash.py image`.
- The on-screen progress bars and `flash.py boot` are wired through the CLI;
  FlashGBLink shows both bars during a FlashGBX write, but booting after a write
  is still a CLI action.
- Cartridges over 32 MiB that need flash bank switching, and the encrypted or
  bus-specific profiles (3D Memory, GBAMP, Sintax, Vast Fame), are not supported.

## Word protocol

All traffic is 32-bit words, MSB-first. The host sends a word and receives one
back; the payload preloads its reply, so a reply to a command word is the
previous status. Addresses below `0x08000000` are relative to the cartridge
base; anything else is used as-is, which is how save memory at `0x0E000000` is
reached with the same commands.

| Word | Meaning |
|---|---|
| `0x5A5A5A5A` | ping; reply is the current status |
| `0x5A20nn` | write `nn` address/value pairs (two words per pair) |
| `0x5A3000` | read: next words are address and count, then `count` dummies |
| `0x5A5000` | write block: next words are address and packed word count, then data |
| `0x5A0060nn` / `0x5A0160nn` | draw `nn`% on the write (green) / verify (cyan) bar |
| `0x5A7000` | read bytes: next words are address and byte count, then dummies |
| `0x5A8000` | write bytes: next words are address and byte count, then packed data |
| `0x5A40nn` | soft reset into the cartridge |
| `0x5A9000` | install a write template: chunk bytes, command count, then 4 words per command |
| `0xA5A50000` | OK |
| `0xA5A500E1` | rejected command |

## Cartridge notes

The M36 tracks Intel command state per 8 MiB bank. The reset (`0x50`, `0xFF`)
must be issued at an address inside the bank being erased or programmed,
otherwise that bank stays in status-register mode and reads return `0x0080`.
The profile's `reset_every` moves the reset to each bank boundary, and every
sector is reset at its own address before erase and programming.

The buffered-write count field is **bytes minus one** (63 for 64 bytes). The
payload substitutes the profile's `BS` token with the configured write window,
so Intel `E8` and AMD `0x26` profiles share the same code.

Profile wait tables are executed after each command, not only at the end of a
sequence: some parts poll the status register after the first unlock write, and
AMD buffered writes finish with `0x2A` followed by data polling at the buffer
address.

The BIOS soft reset only boots the cartridge when the flag at `0x03007FFA` is
zero; multiboot leaves it non-zero.

## Development

Run the hardware-free tests:

```sh
.venv/bin/python3 -m unittest discover -s tests
```

The suite covers the word protocol, CFI decoding, write-plan encoding, the
FlashGBLink device emulation, FlashGBX `Flashcart` detection through the
adapter, and the progress-bar phase logic.

## Credits

FlashGBLink is mostly glue around two excellent upstream projects. Without them
there would be no link and no cartridge knowledge:

- **[FlashGBX](https://github.com/Lesserkuma/FlashGBX)** by **Lesserkuma**
  (GPL-3.0-or-later) — the desktop application, flashcart profile database,
  cartridge detection, CFI decoding, the `LK_Device`/`Flashcart` device model,
  save-type handling and the GUI. `flashgblink/` implements its AGB device
  interface, and the CLI reads FlashGBX's installed profile database; only the
  four small profiles under `tests/fixtures/` are copied for the tests. If you use
  the GUI or rely on its profile database, that is Lesserkuma's work.
- **[gbasend-rs](https://github.com/nullstalgia/gbasend-rs)** by **nullstalgia**
  (MIT OR Apache-2.0) — the RP2040 firmware, the USB↔SPI bridge, the multiboot
  upload protocol and the `gbasend` CLI. The prebuilt UF2s in `firmware/` are
  unmodified copies. The entire Pico/link/upload layer is theirs.

This project adds the GBA-side payload (16-bit and byte-wide cartridge access,
the profile-driven write-template engine, the two progress bars and the soft
reset), the FlashGBLink device adapter, the `flash.py` CLI with its delta image
writer, and the hardware documentation and validation. The original code and
tests here are roughly 3.8k lines; the value of the project depends heavily on
the two upstream projects above.

Special thanks to **akouzoukos**, developer of
[Apotris](https://github.com/akouzoukos/apotris), for the game used as the
4 MiB image that validated the full write and read-back path.

## Roadmap

- Save backup/restore: the 64 KiB SRAM works; save flash (64/128 KiB, banked)
  and EEPROM (512 B/8 KiB, bit-banged) still to do.
- Flash cartridges that need bank switching (over 32 MiB) and the encrypted or
  bus-specific profiles (3D Memory, GBAMP, Sintax, Vast Fame).
- Faster link: remove the Pico's fixed 100 µs per-word delay and replace it with
  flow control (block acknowledgements or a ready line).
- Optional web UI for auto-updating a ROM over the link.

## License

GPL-3.0-or-later. See `LICENSE` and `NOTICE`.
