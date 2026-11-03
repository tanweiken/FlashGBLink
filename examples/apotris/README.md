# Apotris development loop

Flash the current Apotris build without leaving the console: rebuild in the
container, let [`apotris.sh`](apotris.sh) notice the change, write only the
changed sectors, verify, and boot the new build. Run the commands below from the
repository root.

[Apotris](https://github.com/akouzoukos/apotris) is a GBA Tetris game by
akouzoukos; this example is the loop used while working on it.

For the bootleg `M36L0R8060B` cartridge used for Apotris development, the
profile is fixed, so the helper skips autodetection and flashes the current
build:

```sh
examples/apotris/apotris.sh                  # changed sectors, verify, then boot
examples/apotris/apotris.sh --watch          # flash again after every rebuild
examples/apotris/apotris.sh --full           # rewrite every sector
examples/apotris/apotris.sh --no-verify      # skip the read-back
examples/apotris/apotris.sh --no-boot        # batch mode: no test boot
examples/apotris/apotris.sh --interval 0.5   # check the ROM twice a second
examples/apotris/apotris.sh --dry-run        # print the command without flashing
```

`--watch` polls the ROM, flashes whenever it changes, and boots the cartridge so
the new build can be tested. Idle polling only checks mtime, size and inode; the
full checksum is computed only after that stamp changes. Polling defaults to
once per second (`--interval` or `APOTRIS_POLL_INTERVAL`). Booting ends the
payload, so before the next rebuild power on the GBA with START+SELECT held; the
watcher uploads the payload again, then flashes and boots. The upload waits up
to `APOTRIS_HANDSHAKE_WAIT` seconds (default 120).

The `apotris_builder` container bind-mounts the checkout at `/root/apotris`, so
its build output appears at `$HOME/apotris/build-gba/Apotris.gba` on the host:

```sh
docker exec -w /root/apotris apotris_builder \
  sh -lc 'meson compile -C build-gba'
```

`--no-boot` skips the boot and leaves the payload resident, which is useful for
flashing several builds in a row, but it does not let you test the game.

It reads `$HOME/apotris/build-gba/Apotris.gba` by default, resolves `gbasend`
from `PATH` or the local gbasend-rs build, and uses the profile
`2006-36-71_V2 with M36L0R8060B`. Override the defaults with `--rom`,
`--profile`, `--gbasend`, `--port`, `--baud`, `--upload-baud`, `--payload`,
`--config`, `--handshake-wait`, or `--interval`.

After a successful flash, those options are saved to
`~/.config/flashgbblink/apotris.conf`, so later runs reuse them. Set
`APOTRIS_SETTINGS` to use a different file. Environment variables such as
`APOTRIS_ROM`, `APOTRIS_PROFILE`, `APOTRIS_GBASEND`, `APOTRIS_PORT`,
`APOTRIS_BAUD`, `APOTRIS_UPLOAD_BAUD`, `APOTRIS_PAYLOAD`,
`APOTRIS_HANDSHAKE_WAIT`, and `APOTRIS_POLL_INTERVAL` override saved values;
command-line options override both. Delete the settings file to return to the
built-in defaults.

## Adapting it to your own project

The watch/flash/boot loop is generic; only the Apotris names are not. For another
homebrew project, copy `apotris.sh` next to your own launcher and change:

- `APOTRIS_ROOT` (or `--rom`) to the path your build writes, and the
  `apotris.conf` settings file name;
- `DEFAULT_PROFILE` to the profile for your cartridge, or drop the forced
  `--profile` and let `flash.py image` autodetect it.

Everything else — the mtime/inode stamp, the delay until the file stops
changing, the `--watch` upload retry after a boot, and the remembered settings —
works unchanged. The helper stays specific to one fixed cartridge; pass
`--profile` and `--config` if yours needs a local profile copy.

## Detecting and fixing the cartridge profile

The helper hardcodes the profile validated for this cartridge. If you use a
different cartridge, or detection starts failing, identify it with the payload
running:

```sh
PY=.venv/bin/python3

$PY flash.py detect
$PY flash.py id
$PY flash.py cfi
$PY flash.py profiles --verbose
```

- `id` prints the flash chip's JEDEC ID. The tested 32 MiB M36 reports
  `20 00 0E 88 02 00 FF FF`.
- `detect` prints the matched profile, command set, flash ID and CFI. If it
  says `no profile matched`, use the ID to find a profile manually.
- `profiles --verbose` prints every profile name, its aliases and its
  `fc_AGB_*.txt` file.
- `cfi` prints the chip size, voltage and sector map. Check that the profile's
  `flash_size` and sector layout agree with it.

Select a profile explicitly when autodetection is wrong or impossible:

```sh
examples/apotris/apotris.sh --profile "PROFILE NAME"

# The same option works with the raw CLI:
$PY flash.py image Apotris.gba --profile "PROFILE NAME" --boot
```

After a successful flash, the helper remembers `--profile` and `--config` along
with the path and transport options. You do not need to repeat them on the next
run. If several profiles share an ID, choose the one whose PCB or flash-chip
name matches the cartridge label.

If no installed profile matches, copy the closest `fc_AGB_*.txt` from
FlashGBX's config directory into a local directory, for example
`~/apotris-flash-profiles/`, then edit it:

1. Put the display name first in `names`; that is what `--profile` accepts and
   what detection prints.
2. Put the 4-byte ID from `flash.py id` in `flash_ids`. Several IDs are allowed.
3. Keep `command_set`, `flash_size`, `sector_size_from_cfi`, `reset_every` and
   `commands` from the closest known chip. For the M36, `reset_every` is
   `0x100000` because its command state is tracked per 8 MiB bank.
4. Point the CLI at the local directory:

```sh
examples/apotris/apotris.sh --config ~/apotris-flash-profiles \
  --profile "YOUR PROFILE NAME"
```

The directory only needs to contain the `fc_AGB_*.txt` files you want; it does
not need a copy of the whole FlashGBX configuration. `FLASHGBLINK_CONFIG` is
the equivalent environment override.

If identification and CFI look right but writes fail, compare the profile's
command sequences with the flash chip datasheet: `reset`, `sector_erase`,
`buffer_write`, and their wait tables. Change one field at a time and verify
with `flash.py read` or a verified `flash.py image` run on a cartridge whose
contents you can lose.
