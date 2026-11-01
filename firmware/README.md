# gbasend-pico firmware

Prebuilt RP2040 UF2 images from
[nullstalgia/gbasend-rs](https://github.com/nullstalgia/gbasend-rs), commit
`5b20987ec5b859cb8d8012f175501037c0598880` (2026-02-25), downloaded
2026-11-01.

| File | Use |
|---|---|
| `gbasend-pico.uf2` | plain Pico / RP2040 board |
| `gbasend-pico-rgb.uf2` | board with a WS2812 LED on GP16 |

SHA-256:

```
c0f9cd3856d76c4387c30529f795ced955c133c39c1e9355e411139a6a219d17  gbasend-pico.uf2
1570e07ec407b471a5e2bc9df34652013bcdd1b101d77edbd247bb391b75d510  gbasend-pico-rgb.uf2
```

Both images report family `Raspberry Pi RP2040` (UF2 family ID `0xe48bff56`).
They do not run on RP2350 boards (Pico 2, RP2350-LCD-1.47) or ESP32.

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

## License

Dual MIT / Apache-2.0, per `gbasend-pico/LICENSE-MIT` and
`gbasend-pico/LICENSE-APACHE-2.0` in the upstream repository.
