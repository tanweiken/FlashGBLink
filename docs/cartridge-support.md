# Cartridge support

This page describes what FlashGBLink can read and write today. It is written
for the CLI and for the FlashGBX plugin; the plugin uses FlashGBX's own
dialogs for most operations.

## Consoles and cartridges

- **Game Boy Advance cartridges only.** GB and GBC cartridges need the GBA in
  Game Boy Color mode, 5 V link signalling and a different payload; that is
  not implemented.
- The GBA must be powered on with **START + SELECT** held for the multiboot
  upload. The payload stays in RAM until reset or power-off.

## Detection

1. FlashGBX profiles from the installed configuration directory, in file
   order. This is the same profile set the FlashGBX GUI uses.
2. If no profile matches, the **Burn2Slot-style fallback** tries the six
   common AMD unlock layouts and six data-line wirings, plus the Intel ID
   sequence. A match produces a synthetic profile for this session.

Fallback detection sends only identification/reset commands; it does not erase
or program data. Writing a cartridge found this way needs `--fallback-write`,
and the cartridge should be backed up first. The synthetic profile has no
hardware validation; treat it as a last resort.

Profiles that need cartridge power cycling, external bank switching, encrypted
access or a different bus (3D Memory, GBAMP, Sintax, Vast Fame) are not
supported for writing.

## ROM writing

- Sector erase by default, using the profile's sector map or the chip's CFI.
- `--chip-erase` erases the whole chip first, for profiles with a chip-erase
  command and no usable sector map.
- Only changed sectors are erased and programmed. A state file under
  `~/.cache/flashgblink/delta/` makes a repeat write of an unchanged image
  fast; `--full` ignores the state and rewrites everything.
- Each programmed sector is read back; the payload's CRC32 command is used
  when available, with a byte comparison to explain a mismatch.
- The image is padded to the end of its last sector. **Data after the ROM is
  not erased**, matching FlashGBX: after writing a smaller image, a later dump
  can include stale data after the ROM.
- A cartridge requiring a power cycle between operations aborts with a prompt.

## Saves

- **SRAM:** dump and restore are supported. The CLI defaults to a 64 KiB save;
  use `--save-type sram-32k|sram-64k|sram-128k` (or `--size`) when the
  cartridge uses a different size.
- **EEPROM (512 B and 8 KB): not supported.** The payload has no EEPROM
  bit-banging path. An EEPROM backup attempt fails with an explicit error
  rather than producing a blank `.sav`.
- **Save flash (64/128 KB): not supported.** Dump and restore fail explicitly.
- **RTC:** not read or written. A cartridge with a real-time clock is reported
  as having no RTC.
- `restore` requires the file to be **exactly** the size of the save media.
  A short file is refused because it would leave the rest of the save stale.

## Link integrity

- ROM and save reads use the payload's checked-read commands when the running
  payload supports them. A block whose checksum does not match is retried; a
  persistent failure raises instead of returning data that might be corrupt.
- A payload from an older release has no checked-read commands. FlashGBLink
  falls back to unchecked reads and warns once. Re-upload
  `payload/flasher.gba` from the same release to enable the check.
- A cancelled write stops between blocks, asks the payload to abort, removes
  the delta state and leaves the partially programmed sector for the next run
  to erase and repair.

## Hardware tested

- Bootleg 32 MiB `M36L0R8060B` (FlashGBX profile `2006-36-71_V2`): detection,
  ROM reads, erase/program, full-image writes with verification, boot, and
  64 KiB SRAM backup/restore.
- Other profiles execute the same profile-driven write path but have not been
  validated on real hardware here.
- Unknown-cart fallback and chip erase are covered by software tests only; no
  real unknown cart has been used for validation.
