# gbasend-pico firmware

Prebuilt RP2040 UF2 images from
[nullstalgia/gbasend-rs](https://github.com/nullstalgia/gbasend-rs), commit
`a72da50` (2026-11-02), plus the local SO flow-control change described below
(not yet upstream), built and copied in on 2026-11-03.

| File | Use |
|---|---|
| `gbasend-pico.uf2` | plain Pico / RP2040 board |
| `gbasend-pico-rgb.uf2` | board with a WS2812 LED on GP16 |

SHA-256:

```
d3d7021e1efec7f5c68a17b55cb2b8cb160a12fd37574f12c3578b782d99edc1  gbasend-pico.uf2
c2ab26077989698dd103d683d10fd4310f1377880039e8c8e41ea6acd061cbb6  gbasend-pico-rgb.uf2
```

Both images report family `Raspberry Pi RP2040` (UF2 family ID `0xe48bff56`).
They do not run on RP2350 boards (Pico 2, RP2350-LCD-1.47) or ESP32.

## SO flow control

Each word used to cost a fixed delay because the bridge could not tell whether
the GBA had loaded its next reply. The GBA's payload now drives the SIO SO line
high while it is busy and low once the reply is in the data register, and the
bridge waits for SO low before clocking (2 µs arm, 5 µs settle). A transfer
therefore costs as long as the GBA's own work plus 32 clocks, instead of a
fixed 150 µs per word.

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
git clone https://github.com/nullstalgia/gbasend-rs
cd gbasend-rs/gbasend-pico
cargo build --release
picotool uf2 convert target/thumbv6m-none-eabi/release/gbasend-pico \
  -t elf gbasend-pico.uf2
```

The bundled images also need the SO flow-control working tree change on top of
the commit above; a stock `a72da50` build does not read SO and will pace words
with the fixed delay.

## License

Dual MIT / Apache-2.0, per `gbasend-pico/LICENSE-MIT` and
`gbasend-pico/LICENSE-APACHE-2.0` in the upstream repository.
