# gbasend-pico firmware

Prebuilt RP2040 UF2 images built from
[tanweiken/gbasend-rs](https://github.com/tanweiken/gbasend-rs) commit
`b68a37d` (2026-11-04), a fork of
[nullstalgia/gbasend-rs](https://github.com/nullstalgia/gbasend-rs) (base
`a72da50`, upstream `main` merged in `c169302`) that adds the SO ready-line
handshake described below.

| File | Use |
|---|---|
| `gbasend-pico.uf2` | plain Pico / RP2040 board |
| `gbasend-pico-rgb.uf2` | board with a WS2812 LED on GP16 |

SHA-256:

```
86f7cd566c12bdf440f2d55f9a99acb4171e1d083235633f24aad40846ce2083  gbasend-pico.uf2
26aaec90cd552de2d0fc93cbfe2bfee694c339d4d6b9346f85879bf3e7d40a6c  gbasend-pico-rgb.uf2
```

Both images report family `Raspberry Pi RP2040` (UF2 family ID `0xe48bff56`).
They do not run on RP2350 boards (Pico 2, RP2350-LCD-1.47) or ESP32.

## SO flow control

Each word used to cost a fixed delay because the bridge could not tell whether
the GBA had loaded its next reply. The GBA's payload now drives the SIO SO line
high while it is busy and low once the reply is in the data register, and the
bridge waits for SO low before clocking (5 µs arm, 20 µs settle). A transfer
therefore costs as long as the GBA's own work plus 32 clocks, instead of a
fixed 150 µs per word.

The margins started at 2 µs and 5 µs. On hardware the handshake then failed
above about 900 kHz with data mismatches once the clock reached the payload's
own processing time, so `b68a37d` widened them to 5 µs and 20 µs; the same speed
test then ran clean at 1.0, 1.5 and 1.8 Mbaud (9,727 / 10,831 / 11,071 words per
second) and only broke at 1.9 Mbaud. The wider settle costs about 18 µs per word
at a given clock, so the clock is now the lever: raise `--baud` rather than
expect the old 1.25 Mbaud timing.

The bridge starts with the original fixed delay and only probes for the
handshake at or above 1 Mbaud, so the BIOS multiboot phase — which uses SO as its
reply data line rather than as a ready line — is never clocked at flow-controlled
speed. A low line alone is not accepted as proof: the bridge watches for the GBA
to pull SO high after a word, which only a payload that implements the handshake
does. If no busy edge appears within 2 s it falls back to the fixed delay for
that session and tries again at the next session boundary (baud change or a
second of USB idle). A steady-state wait is capped at 10 s so a dead GBA cannot
wedge the bridge.

Any pairing is safe:

| Bridge | Payload | Result |
|---|---|---|
| new | new | SO-paced, the fast path |
| new | old | old payload never drives SO, so no busy edge is seen; fixed delay after ~2 s of probing |
| old | new | the old bridge does not read SO; fixed delay |

The supported path is still to ship the payload, plugin zip and UF2 images
together, since only the new payload produces the busy edge the probe looks for.

`gbasend-lib/speed-test-gba/src/main.rs`, `nootloader/` and `xboo-rs` in
gbasend-rs carry the same change.

## Flash

1. Hold BOOTSEL and plug the Pico into USB.
2. Copy the UF2 to the mounted `RPI-RP2` drive.
3. The Pico reboots as a USB serial device with VID:PID `2E8A:000A`, which
   `flash.py` autodetects.

## Wiring

The firmware uses hardware SPI0: GP0 = MISO (GBA SO), GP2 = SCLK (GBA SC),
GP3 = MOSI (GBA SI), plus GND. The upstream
[DIY guide](https://github.com/nullstalgia/gbasend-rs/blob/main/DIY.md) uses a
1 kOhm series resistor and a Game Boy Color link cable.

Power the Pico from USB only. Do not connect USB and the link port's VCC at the
same time.

## Rebuilding

```sh
git clone https://github.com/tanweiken/gbasend-rs
git -C gbasend-rs checkout b68a37d
cd gbasend-rs/gbasend-pico
cargo build --release
picotool uf2 convert target/thumbv6m-none-eabi/release/gbasend-pico \
  -t elf gbasend-pico.uf2
```

Upstream `nullstalgia/gbasend-rs` at `a72da50` does not read SO and paces every
word with the fixed delay, so it will not reproduce these images.

## License

Dual MIT / Apache-2.0, per `gbasend-pico/LICENSE-MIT` and
`gbasend-pico/LICENSE-APACHE-2.0` in the upstream repository.
