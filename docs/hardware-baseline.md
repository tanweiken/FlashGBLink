# Hardware baseline

Recorded 2026-11-01 before the save backup/restore work, so a payload or
protocol change can be compared against a known-good run.

## Setup

| Item | Value |
|---|---|
| Cartridge | bootleg 32 MiB `M36L0R8060B` (FlashGBX profile `2006-36-71_V2`) |
| Console | GBA, link port |
| Bridge | RP2040 Pico, `gbasend-pico.uf2` (gbasend-rs `5b20987`) |
| Payload | `payload/flasher.gba`, 1168 bytes, sha256 `54ed33b3d85a5212b7753f504adf0f9342d4b0ea800c9f45760827b446459991` |
| Host | `flash.py` at commit `10fcff2`, default `--baud 1250000`, `--upload-baud 256000` |

Unit tests pass (8/8 at the time of this baseline; 33 with the profile tests).

## Identity

`flash.py id`:

```
20 00 0E 88 02 00 FF FF
```

`flash.py cfi`:

```
CFI signature: OK
swapped pins: [(0, 1)]
size: 0x2000000 (32.00 MiB)
voltage: 1.7-2.0 V
region 0x0000000-0x1FDFFFF: 0x20000 x 255
region 0x1FE0000-0x1FFFFFF: 0x8000 x 4
```

## Timings

| Operation | Measured | README |
|---|---|---|
| multiboot upload | 1.5 s | ~2 s |
| `image Apotris.gba --full --boot` (32 sectors, 4 MiB, verify) | 6:03.07 | 6:03 |
| full-density sector (erase, 128 KiB program, verify) | ~14.6 s | ~11 s |
| `read` 512 bytes | ~2 s | — |

The README's 6:03 is reproduced exactly. Its ~11 s per sector holds for the
Apotris image because sectors 21-32 are `0xFF` padding and skip programming;
a sector that actually receives 128 KiB costs ~14.6 s, which is what the
Golden Sun run measured and what a full 8 MiB image would cost (~12 min).

With the progress bar added, the same Apotris run took 6:17.07 — the 14 s
difference was 32 progress updates each burning a 0.5 s reply timeout while the
bar was still drawing. `progress()` now waits 10 ms before pinging; re-measured
at 6:07.09.

## Known failure (fixed): short transfer desyncs the link

During a `--full` write of an 8 MiB image, sector 25's verify read failed:

```
[25/64] sector 0x300000 ... wrote 131072 byteserror: timeout waiting for 4112 reply bytes (got 3844)
```

`Link.xfer` used to wait for `len(words) * 4` bytes and raise on a short count
without draining, so the remaining replies stayed in the OS serial buffer and
offset every later transfer. The payload, meanwhile, sat inside `sio_xfer()`
waiting for clocks that never came.

At the time this looked like a wedged Pico (`gbasend --speed-test` failed on
every method until a USB replug). A forced stall on the same hardware after the
fix shows otherwise: `flash.py` recovered in 0.22 s without touching the Pico,
so the replug was only clearing the host-side buffer.

### What `flash.py` does now

- `Link.__init__` flushes the input buffer, so bytes left by an earlier aborted
  run cannot offset the first reply.
- `xfer` raises `TransferError` carrying how many words were missing.
- `xfer_with_recovery` drains, clocks `RESYNC_WORDS` (1100) dummy words at the
  payload — they are either its remaining replies or invalid headers it
  rejects — then pings until the payload answers, and retries the command once.
- If that fails, the error names the manual recovery: replug the Pico, then
  power-cycle the GBA holding START+SELECT.

A desync during a block write can program a partial block, which the existing
verify path catches and rewrites after re-erasing the sector. A host process
killed outright still needs the GBA power-cycled, since nothing can clock the
payload again.

## Save media

The save is at `0x0E000000`, 64 KiB, and it is **8 bits wide**. Reading it with
the cartridge's 16-bit path returns the same byte in both halves of the lane:
all 32768 byte pairs of a 64 KiB dump came back identical (`0a 0a` where the
emulator save has `0a 00`), so a `.sav` written that way is unreadable by an
emulator.

`cmd_read8`/`cmd_write8` (opcodes `0x70`/`0x80`) do byte-wide access and pack
four bytes per reply word. After the switch the dump lines up with the emulator
save's structure (`S` version byte plus little-endian 32-bit counters), with
content differing only where the two play sessions diverge.

Checks run on hardware:

- Dump and immediate restore: same sha256 before and after.
- Writes are real, not no-ops: a marker written to offset 0 and to `0x8000`
  read back intact, so the upper 32 KiB is genuine media, not a mirror.
- The original save was restored afterwards and its sha256 re-checked.

Timings: dump 64 KiB in ~2.6 s, verified restore in ~6.8 s. Byte stores to this
cart's save bus cost far more than the link transport, so restore is dominated
by the store loop, not by SPI.

## Progress bar

The payload draws a bar on the GBA in video mode 3 (`screen_init`), and the host
sends `0x5A60nn` after each sector and during the diff read. Observed filling to
100% before `--boot` hands the screen back to the cartridge. Drawing outlasts
the bridge's inter-word delay, so progress is fire-and-confirm: the host sends
the update, waits 10 ms, and pings; a ping sent during the draw would otherwise
burn a full reply timeout (~0.5 s per sector).

## How to reproduce

```sh
PY=.venv/bin/python3
$PY flash.py upload --gbasend ../gbasend-rs/gbasend-cli/target/release/gbasend
$PY flash.py id
$PY flash.py cfi
$PY flash.py image Apotris.gba --full --boot
```

## FlashGBX profile update

The measurements above were taken with the first payload, before profile
support. The payload now holds a write template instead of an Intel-only
buffered-write loop, so its size and hash changed:

| Item | Now |
|---|---|
| Payload | `payload/flasher.gba`, 3216 bytes, sha256 `c3d959a4bbb2c0a02de9e16368b8cef2dfa792f1ce7c52b019cbe253d2c05caf` |
| New opcode | `0x5A9000` install write template (chunk bytes, command count, 4 words per command) |
| Host | `flash.py` autodetects the profile, then installs its buffer/single-write template |

The M36 identity, CFI and timings above still apply: the detected profile is
`fc_AGB_M36L0R806.txt`, whose buffered-write sequence is the same `E8`/`D0`/`FF`
pattern the first payload hard-coded. The sector map now comes from that
profile's CFI data (255 x 128 KiB + 4 x 32 KiB on this cartridge), and the state
file records the map as well as the per-sector hashes.

## FlashGBLink device backend

Recorded 2026-11-01 on the same setup, after switching the host to FlashGBX
5.2.1 with the `flashgblink` plugin and a 4 MiB Apotris build
(`~/apotris/build-gba/Apotris.gba`, sha256
`dbf1306181de4a65ff16fe9c987aa9adb9411636ec511544d7d7b155316f49e6`).

| Check | Result |
|---|---|
| `GbxDevice.Initialize` | finds `2E8A:000A`, AGB mode |
| `ReadHeader` | `APOTRIS`, code `2ATE`, ROM size 0x2000000 |
| `DetectFlash` | selects `2006-36-71_V2 with M36L0R8060B` |
| 4 MiB read through the adapter | 160.2 s, every 64 KiB chunk matches the ROM |
| SRAM read through the adapter | first 4 KiB matches the `flash.py save` dump |
| Full write through the adapter | 407.8 s first run, 403.9 s second run, both verified |
| Independent read-back after write | header, first/last 64 KiB, then all 4 MiB match |
| GBA progress bars | green write bar and cyan verify bar advance in 2% steps |

The adapter's write path erases every sector in the image (FlashGBX behaviour);
the `.flash-state.json` delta fast path remains a `flash.py image` feature.  The
state file still describes the previous Apotris image after these writes, so the
next `flash.py image` reads the cartridge back and refreshes it.
