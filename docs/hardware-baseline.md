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
| Payload | `payload/flasher.gba`, 5120 bytes, sha256 `47e517eabc0f3d6a890e0877529af8016cce253f217cb27b25f96fbce45139ff` |
| New opcodes | `0x5A9000` install write template; `0x5A0060nn` / `0x5A0160nn` write/verify progress bars |
| Host | `flash.py` autodetects the profile, then installs its buffer/single-write template |

The M36 identity, CFI and timings above still apply: the detected profile is
`fc_AGB_M36L0R806.txt`, whose buffered-write sequence is the same `E8`/`D0`/`FF`
pattern the first payload hard-coded. The sector map now comes from that
profile's CFI data (255 x 128 KiB + 4 x 32 KiB on this cartridge), and the state
file records the map as well as the per-sector CRC32s.

## Payload CRC32 (delta detection)

Added 2026-11-04. The payload reports a CRC32 over cartridge bytes, so a diff
scan and a post-program verify cost only the cartridge bus read instead of a
transfer over the link.

| Item | Now |
|---|---|
| Payload | `payload/flasher.gba`, 5504 bytes, sha256 `e7236c480c7446a779bb223b3e2ac1a3936ce0fbfd598d289c9b12a8ebb6945c` |
| New opcodes | `0x5AB000` start a CRC32 job (address, byte count); `0x5AB100` read the result |
| FlashGBX | the plugin answers `CALC_CRC32` (0xD5), so **Config → Skip writing matching ROM chunks** now skips matching sectors |

On the `performance` line the same sources are built with the SO flow control
(`0.3.0b2`): 5872 bytes, sha256
`9d30f8a32a9bbd71e7010faa248ea639ec230ff140b16ef16c011bde66980a05`. The
timings below were measured with the `main` payload on stock firmware.

`flash.py image` compares one CRC per sector (32 round trips for 4 MiB) instead
of reading the image back, and verifies a programmed sector with one CRC
instead of a 128 KiB read. The job is polled and fetched separately because the
Pico clocks the link at a fixed pace: a reply that waited for a whole sector to
be CRC'd would be clocked into the wrong word.

Measured on the M36 cart, 2026-11-04, `main` at 1.25 MHz:

| Operation | Result |
|---|---|
| payload CRC of 128 KiB | 0.54 s (~240 KB/s) |
| same 128 KiB read over the link | 4.3–4.7 s (8–9× slower) |
| CRC correctness | equal to `zlib.crc32` of the read-back bytes, 3 sectors |
| cold 4 MiB diff (no state) | 21–22 s for 32 sectors |
| repeat diff (state hit) | 0.55 s, no reads, no writes |
| one changed deep sector (4 KiB programmed) | 2.4 s, verified, read-back confirmed |
| external change (`program` outside the tool) | probes detect it, CRC scan, sector repaired |
| full 4 MiB write with CRC verify | 2:50.93 (was 6:03–6:07 with a read verify) |
| FlashGBX compare through `CALC_CRC32` | 0.60 s per 128 KiB |
| FlashGBX write, all 32 sectors skipped | 21.5 s (was 403.9 s) |

The `--full` write hit one rejected block (~1 in a few hundred) and the new
sector retry re-erased and rewrote it; the rest of the run was clean. That
rejection is link-level, not CRC related: it reproduces with the CRC command
disabled.

The CRC implementation was checked against `zlib.crc32` on the host for even
and odd lengths as well as on the cartridge.

### Progress/CRC handshake fix

Running the CRC scan on hardware exposed a race that also affected the old
progress bar: the payload shifts out whatever is in `SIODATA32`, so a
confirmation ping that arrives while the bar is still being drawn can read the
previous `ST_OK` and "confirm" an update the payload has not started yet. The
payload is then left waiting for a clock, and the next command is shifted by one
word.

`0x5A0060nn` and `0x5AA000` now preload `0xA5A50001` (busy) before drawing and
`ST_OK` only when they return to the word loop, so a mid-draw reply can never be
mistaken for the confirmation. The host also polls the confirmation every 20 ms
instead of in a tight loop: a ping flood saturates the Pico's USB path and can
byte-shift the reply stream. The CRC command's replies are checked for the echo
status on both argument words, which catches an out-of-frame start instead of
issuing a job over a shifted frame. The payload caps a CRC job at 1 MiB, so a
misread length cannot hold the link for long. A block rejected mid-sector now
re-erases and rewrites the sector instead of aborting the image.


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
| GBA progress UI | title, WRITE/VERIFY labels, percentages, green/cyan bars, FLASH COMPLETE / RESTART GBA TO PLAY |

The adapter's write path erases every sector in the image unless FlashGBX's
**Skip writing matching ROM chunks** is enabled; with the payload's CRC32
command that compare now runs on the cartridge bus, and `flash.py image` uses
the same command for its diff and verify. The state file still describes the
previous Apotris image after a write from FlashGBX, so the next
`flash.py image` checks every sector with a CRC and refreshes it.


## Connect auto-upload

Recorded 2026-11-02.  With the payload absent and the GBA in the BIOS
START+SELECT wait, clicking Connect in FlashGBX opens a cancellable dialog and
runs the bundled `gbasend` against the plugin's `flasher.gba`.

The first `Initialize` after `gbasend` exits can still receive the tail of the
multiboot stream (`0x00FF94EF`, then `0xFFFFFFFF`) instead of a payload status.
`Initialize` now drains and retries the ping for up to 2 s; the GUI also probes
quietly and retries discovery once.  After that the first Connect succeeds
without a manual reconnect.  The plugin writes these events to
`<FlashGBX config>/flashgblink.log`.

## SO flow control (measured 2026-11-03)

Recorded 2026-11-03. The bridge no longer paces words with a fixed delay. The
payload raises SIO SO while it is busy and drops it once the next reply is in
the data register; the Pico waits for SO low before clocking each word, with a
5 µs arm and 20 µs settle. Those margins were 2 µs and 5 µs when the numbers
below were taken: on hardware that build mismatched above roughly 900 kHz, so
`b68a37d` widened them and the same speed test then ran clean to 1.8 Mbaud.

| Item | New |
|---|---|
| Bridge | `firmware/gbasend-pico.uf2` / `-rgb.uf2`, gbasend-rs `a72da50` plus the flow-control change |
| Payload | `payload/flasher.gba`, 5456 bytes, sha256 `7fccb2b6fdf4f769717c89f6ca63df59a02d52c0f94ef5e748b177b093f84d45` |
| Host | `Link.xfer` no longer calls `ser.flush()` per exchange |

`0.3.0b2` rebuilds this payload together with the CRC32 work from `main`
0.2.3: 5872 bytes, sha256
`9d30f8a32a9bbd71e7010faa248ea639ec230ff140b16ef16c011bde66980a05`. The
measurements below were taken with the b1 payload (no CRC32).

The payload change is a handful of register writes per word: assert SO with
start clear, load `SIODATA32`, drop SO for the ready window, set start, then
assert SO again once the word is in. The first build of the change was 6048
bytes because it did a read-modify-write of the volatile control register; a
whole-word write each time brings it to 5456 bytes with arm-none-eabi-gcc 15.2.1
(Arm GNU Toolchain 15.2.rel1). The same sources without the change build to 5136
bytes with that toolchain; the 5120-byte image this project shipped before was
built with an older one.

### Measured

Same cartridge, same Pico, `gbasend-pico.uf2` with the handshake and the 2 µs /
5 µs margins, payload above, `flash.py` at 1.25 Mbaud. Wall clock, one run each:

| Operation | Baseline | Now | Change |
|---|---|---|---|
| multiboot upload | 1.5 s | 1.8 s | unchanged (BIOS pacing, plus process start) |
| `read` 512 bytes | ~2 s | 0.07 s | ~28× |
| 4 MiB read (64 KiB commands) | 160.2 s (plugin) | 45.1 s | **3.6×**, 90.8 KiB/s |
| 4 MiB `image --full --boot` | 6:03 (363 s, CLI) | 2:13 (133 s) | **2.7×**, verified |
| 4 MiB full write (plugin) | 404 s | 2:22 (142 s) | **2.8×**, verified |

The read rate is 43 µs per word: 25.6 µs of clock at 1.25 Mbaud, 7 µs of bridge
arm and settle, and the rest payload work and USB framing. The 156 KiB/s clock
ceiling is therefore still 1.7× away, which is what the later phases (fewer
round trips, double buffering, a faster clock) would recover.

The 5 µs / 20 µs margins cost about 18 µs more per word than the 2 µs / 5 µs
ones these numbers were taken with, so at a fixed 1.25 Mbaud the 4 MiB read
should land near 61 µs per word (~65 KiB/s) until the clock is raised. With the
widened margins the speed test ran at 9,727 / 10,831 / 11,071 words per second at
1.0 / 1.5 / 1.8 Mbaud and failed at 1.9, so `--baud` in the 1.5–1.8 Mbaud range
is the setting to measure next — the speed-test ROM's own per-word work is the
floor there, not the link.

### Wider margins at 1.8 Mbaud

Measured 2026-11-04 with `b68a37d`, same cartridge, payload and 64 KiB read
commands:

| Clock | 4 MiB read | Rate | Per word |
|---|---|---|---|
| 1.25 Mbaud | 58.4 s | 70.1 KiB/s | 55.7 µs |
| 1.5 Mbaud | 54.2 s | 75.6 KiB/s | 51.7 µs |
| 1.8 Mbaud | 52.1 s | 78.6 KiB/s | 49.7 µs |

All three reads returned the same sha256, so 1.8 Mbaud is clean here; the speed
test's first mismatch is at 1.9 Mbaud. A full `image --full --boot` of the same
4 MiB Apotris image at 1.8 Mbaud took **1:54** wall clock with the payload's CRC
verify, against 2:13 at 1.25 Mbaud with the old read-back verify and 6:03 in the
0.2.2 baseline.

The wider margins cost about 13 µs per word against the 2 µs / 5 µs build
(55.7 µs vs 43 µs at 1.25 Mbaud), and 1.8 Mbaud gives back roughly half of that
(49.7 µs). 1.8 Mbaud is the fastest clean setting measured; 1.9 Mbaud is not.

The 4 MiB write includes erase, program and verify, so it cannot be split from
this run; verify alone accounts for a read's worth of the 133 s. FlashGBX's 2:22
is its own elapsed-time readout for the same operation and within 7% of the CLI,
even though the plugin hands the payload 1 KiB chunks where the CLI uses 64 KiB.

One host-side limit surfaced while measuring: `flash.py read` builds a single
frame for the whole length, so a multi-megabyte read fills the serial write
buffer, times out, and leaves the payload inside the read command. `read` now
clocks in 4 KiB regions like the plugin; recovering an already-wedged session
means clocking out the outstanding words, or power-cycling the GBA.

### What to check first

1. Flash the new UF2, then `flash.py upload` and `flash.py id`; a wrong SO
   ordering shows up here as a timeout or a shifted reply rather than silent
   corruption. The upload runs at 256 kHz, below the 1 Mbaud probe threshold, so
   this step must behave exactly as before the change.
2. `flash.py cfi`, a 512-byte `read`, then a 64 KiB save dump and its verified
   restore.
3. One full-density sector (erase, 128 KiB program, verify).
4. `flash.py image Apotris.gba --full --boot`, then a FlashGBX write through the
   plugin.
5. If timings do not move, the handshake is not engaging: check SO on GP0 with a
   logic analyzer, and confirm the GBA runs this payload rather than a cached
   copy. The probe needs to catch the busy edge, which for a ping is only a few
   hundred nanoseconds wide — a missed edge costs the speedup, not correctness.

### Probe limits

The probe starts off, only runs at or above 1 Mbaud, and only locks on after
seeing SO go high following a word:

| Situation | SO behaviour | Probe outcome |
|---|---|---|
| New payload, idle or with a word loaded | high while busy, low when ready | busy edge seen, engages |
| New payload with the old bridge | the old bridge does not read SO | fixed delay, works |
| Old payload (never drives SO) | low, permanently | no busy edge, fixed delay after ~2 s |
| BIOS multiboot | SO is the reply data line | never probed: below 1 Mbaud |

A payload session opened below 1 Mbaud keeps the fixed delay; that is the only
cost of the guard. The previous images are in this repository's history:

```sh
git log --oneline -- firmware/gbasend-pico.uf2   # commit before the change
git show <commit>:firmware/gbasend-pico.uf2 > gbasend-pico-old.uf2
```

The old payload/bridge pair still works (fixed delay), so a regression here is
a rollback of the two UF2 files, not a lost cartridge.
