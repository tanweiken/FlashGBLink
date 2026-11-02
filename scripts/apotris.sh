#!/usr/bin/env bash
# Flash the current Apotris GBA build through FlashGBLink.
#
# The cartridge is a fixed bootleg M36L0R8060B, so this wrapper always passes
# its FlashGBX profile instead of autodetecting. Override APOTRIS_PROFILE if the
# cartridge changes.

set -eo pipefail

usage() {
	cat <<'EOF'
Usage: scripts/apotris.sh [options] [-- extra flash.py image options]

Flash the Apotris GBA build with the fixed M36L0R8060B profile.

Options:
  --watch            keep running and flash again whenever the ROM changes
  --full             rewrite every sector instead of only changed sectors
  --no-verify        skip the read-back verification
  --boot             boot the cartridge after each write (default)
  --no-boot          keep the payload resident for batch flashing
  --dry-run          print the command without running it
  --rom PATH         use a different .gba image
  --profile NAME     use a different FlashGBX profile
  --gbasend PATH     use a different gbasend binary
  --port PORT        serial port for flashing and upload
  --config DIR       FlashGBX profile directory instead of autodetect
  --handshake-wait N seconds to wait for the START+SELECT upload (default 120)
  --interval S       seconds between ROM checks while watching (default 1)
  -h, --help         show this help

Watch mode boots after each flash. Booting ends the payload, so before the next
rebuild power on with START+SELECT; the watcher uploads it again, flashes, and
boots. Use --no-boot only for batch writing without testing.

Environment:
  APOTRIS_ROOT       Apotris checkout (default: $HOME/apotris)
  APOTRIS_ROM        full path to the .gba image
  APOTRIS_PROFILE    FlashGBX profile name
  APOTRIS_GBASEND    path to gbasend
  APOTRIS_HANDSHAKE_WAIT  seconds to wait for START+SELECT (default 120)
  APOTRIS_POLL_INTERVAL   seconds between ROM checks (default 1)

Build the GBA ROM in the Apotris Docker container:

  docker exec -w /root/apotris apotris_builder sh -lc 'meson compile -C build-gba'

For one-shot use the GBA payload must already be running. Upload it once after
powering on with START+SELECT held:

  .venv/bin/python3 flash.py upload

Watch mode does that upload automatically when it is needed.
EOF
}

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PYTHON=${PYTHON:-$ROOT/.venv/bin/python3}
APOTRIS_ROOT=${APOTRIS_ROOT:-$HOME/apotris}
ROM=${APOTRIS_ROM:-$APOTRIS_ROOT/build-gba/Apotris.gba}
PROFILE=${APOTRIS_PROFILE:-2006-36-71_V2 with M36L0R8060B}

if [[ -n ${APOTRIS_GBASEND:-} ]]; then
	GBASEND=$APOTRIS_GBASEND
elif command -v gbasend >/dev/null 2>&1; then
	GBASEND=$(command -v gbasend)
elif [[ -x $ROOT/../gbasend-rs/gbasend-cli/target/release/gbasend ]]; then
	GBASEND=$ROOT/../gbasend-rs/gbasend-cli/target/release/gbasend
else
	GBASEND=gbasend
fi

boot=1
watch=0
dry_run=0
handshake_wait=${APOTRIS_HANDSHAKE_WAIT:-120}
poll_interval=${APOTRIS_POLL_INTERVAL:-1}
flash_args=()
common_args=()

while (($#)); do
	case $1 in
		--full|--no-verify)
			flash_args+=("$1")
			shift
			;;
		--watch)
			watch=1
			shift
			;;
		--boot)
			boot=1
			shift
			;;
		--no-boot)
			boot=0
			shift
			;;
		--dry-run)
			dry_run=1
			shift
			;;
		--port|--upload-baud|--payload|--config)
			[[ $# -ge 2 ]] || { echo "error: $1 needs a value" >&2; exit 2; }
			common_args+=("$1" "$2")
			shift 2
			;;
		--handshake-wait)
			[[ $# -ge 2 ]] || { echo "error: --handshake-wait needs seconds" >&2; exit 2; }
			handshake_wait=$2
			shift 2
			;;
		--interval)
			[[ $# -ge 2 ]] || { echo "error: --interval needs seconds" >&2; exit 2; }
			poll_interval=$2
			shift 2
			;;
		--verbose)
			common_args+=(--verbose)
			shift
			;;
		--rom)
			[[ $# -ge 2 ]] || { echo "error: --rom needs a path" >&2; exit 2; }
			ROM=$2
			shift 2
			;;
		--profile)
			[[ $# -ge 2 ]] || { echo "error: --profile needs a name" >&2; exit 2; }
			PROFILE=$2
			shift 2
			;;
		--gbasend)
			[[ $# -ge 2 ]] || { echo "error: --gbasend needs a path" >&2; exit 2; }
			GBASEND=$2
			shift 2
			;;
		-h|--help)
			usage
			exit 0
			;;
		--)
			shift
			flash_args+=("$@")
			break
			;;
		*)
			flash_args+=("$1")
			shift
			;;
	esac
done

if ! [[ $poll_interval =~ ^([0-9]+([.][0-9]+)?|[.][0-9]+)$ ]]; then
	echo "error: --interval must be a positive number of seconds" >&2
	exit 2
fi

if [[ ! -x $PYTHON ]]; then
	echo "error: $PYTHON not found; create the virtualenv with:" >&2
	echo "  python3 -m venv .venv && .venv/bin/pip install pyserial FlashGBX" >&2
	exit 1
fi

if [[ ! -f $ROM ]]; then
	echo "error: $ROM not found; build the Apotris GBA target first:" >&2
	echo "  docker exec -w /root/apotris apotris_builder sh -lc 'meson compile -C build-gba'" >&2
	exit 1
fi

if [[ ! -x $GBASEND ]]; then
	echo "error: $GBASEND is not executable; build gbasend-rs or pass --gbasend" >&2
	exit 1
fi

command=("$PYTHON" "$ROOT/flash.py" image "$ROM"
	--profile "$PROFILE" --gbasend "$GBASEND")
if ((boot)); then
	command+=(--boot)
fi
command+=("${common_args[@]}")
command+=("${flash_args[@]}")

upload_command=("$PYTHON" "$ROOT/flash.py" upload
	--gbasend "$GBASEND" --handshake-wait "$handshake_wait")
upload_command+=("${common_args[@]}")

if ((dry_run)); then
	printf '%q ' "${command[@]}"
	printf '\n'
	exit 0
fi

OS_NAME=$(uname -s)

file_stamp() {
	case $OS_NAME in
		Darwin) stat -f '%Fm %z %i' "$1" 2>/dev/null ;;
		*) stat -c '%y %s %i' "$1" 2>/dev/null ;;
	esac
}

file_checksum() {
	cksum "$1" 2>/dev/null
}

wait_for_stable() {
	local stamp next
	stamp=$(file_stamp "$ROM") || return 1
	while :; do
		sleep "$poll_interval"
		next=$(file_stamp "$ROM") || return 1
		if [[ $next == "$stamp" ]]; then
			printf '%s\n' "$next"
			return 0
		fi
		stamp=$next
	done
}

upload_payload() {
	printf 'Power on the GBA holding START+SELECT (waiting up to %ss)...\n' \
		"$handshake_wait"
	"${upload_command[@]}"
}

watch_loop() {
	local last_stamp="" last_checksum="" stamp stable checksum needs_upload=0
	printf 'watching %s for rebuilds every %ss (Ctrl-C to stop)\n' \
		"$ROM" "$poll_interval"
	if ((boot)); then
		echo "each flash boots the cartridge; power on with START+SELECT before the next build"
	else
		echo "batch mode: the cartridge is not booted and the payload stays resident"
	fi
	while :; do
		if [[ -f $ROM ]]; then
			stamp=$(file_stamp "$ROM") || true
			if [[ -n $stamp && $stamp != "$last_stamp" ]]; then
				if stable=$(wait_for_stable); then
					checksum=$(file_checksum "$ROM") || true
					last_stamp=$stable
					if [[ -n $checksum && $checksum != "$last_checksum" ]]; then
						last_checksum=$checksum
						if ((needs_upload)); then
							while ! upload_payload; do
								printf 'upload failed; power on with START+SELECT and press Enter to retry (Ctrl-C to stop)\n' >&2
								read -r _ || return 1
							done
							needs_upload=0
						fi
						printf '\n== %s flashing %s ==\n' \
							"$(date '+%H:%M:%S')" "$(basename "$ROM")"
						if ! "${command[@]}"; then
							if ((!boot)); then
								printf 'error: flash failed; restart the watcher\n' >&2
								return 1
							fi
							printf 'flash failed; trying a fresh payload upload...\n' >&2
							if ! upload_payload; then
								printf 'error: upload failed; restart the watcher\n' >&2
								return 1
							fi
							if ! "${command[@]}"; then
								printf 'error: flash failed after upload; restart the watcher\n' >&2
								return 1
							fi
						fi
						if ((boot)); then
							needs_upload=1
						fi
					fi
				fi
			fi
		fi
		sleep "$poll_interval"
	done
}

if ((watch)); then
	watch_loop
else
	exec "${command[@]}"
fi
