#!/usr/bin/env bash
# Flash the current Apotris GBA build through FlashGBLink.
#
# The cartridge is a fixed bootleg M36L0R8060B, so this wrapper always passes
# its FlashGBX profile instead of autodetecting. Override APOTRIS_PROFILE if the
# cartridge changes.

set -eo pipefail

usage() {
	cat <<'EOF'
Usage: examples/apotris/apotris.sh [options] [-- extra flash.py image options]

Flash the Apotris GBA build with the fixed M36L0R8060B profile.

Options:
  --watch            keep running and flash again whenever the ROM changes
  --full             rewrite every sector instead of only changed sectors
  --no-verify        skip the read-back verification
  --boot             boot the cartridge after each write (default)
  --no-boot          keep the payload resident for batch flashing
  --dry-run          print the command without running it
  --rom PATH         use a different .gba image (saved on success)
  --profile NAME     use a different FlashGBX profile (saved on success)
  --gbasend PATH     use a different gbasend binary (saved on success)
  --port PORT        serial port for flashing and upload (saved on success)
  --baud HZ          data-phase SPI clock (saved on success)
  --upload-baud HZ   multiboot SPI clock (saved on success)
  --payload FILE     GBA payload .gba (saved on success)
  --config DIR       FlashGBX profile directory (saved on success)
  --handshake-wait N seconds to wait for START+SELECT (saved on success)
  --interval S       seconds between ROM checks (saved on success)
  -h, --help         show this help

Watch mode boots after each flash. Booting ends the payload, so before the next
rebuild power on with START+SELECT; the watcher uploads it again, flashes, and
boots. Use --no-boot only for batch writing without testing.

Environment:
  APOTRIS_ROOT       Apotris checkout (default: $HOME/apotris)
  APOTRIS_ROM        full path to the .gba image
  APOTRIS_PROFILE    FlashGBX profile name
  APOTRIS_GBASEND    path to gbasend
  APOTRIS_PORT       serial port
  APOTRIS_BAUD       data-phase SPI clock
  APOTRIS_UPLOAD_BAUD  multiboot SPI clock
  APOTRIS_PAYLOAD    GBA payload .gba
  APOTRIS_HANDSHAKE_WAIT  seconds to wait for START+SELECT (default 120)
  APOTRIS_POLL_INTERVAL   seconds between ROM checks (default 1)
  APOTRIS_SETTINGS   settings file (default: ~/.config/flashgbblink/apotris.conf)

Path and transport options are remembered in the settings file after a
successful flash. Environment variables override saved values, and command-line
options override both. Delete the settings file to return to the defaults.

Build the GBA ROM in the Apotris Docker container:

  docker exec -w /root/apotris apotris_builder sh -lc 'meson compile -C build-gba'

For one-shot use the GBA payload must already be running. Upload it once after
powering on with START+SELECT held:

  .venv/bin/python3 flash.py upload

Watch mode does that upload automatically when it is needed.
EOF
}

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
PYTHON=${PYTHON:-$ROOT/.venv/bin/python3}
DEFAULT_PROFILE="2006-36-71_V2 with M36L0R8060B"
SETTINGS_FILE=${APOTRIS_SETTINGS:-${XDG_CONFIG_HOME:-$HOME/.config}/flashgbblink/apotris.conf}
APOTRIS_ROOT=${APOTRIS_ROOT:-$HOME/apotris}

S_PROFILE=""
S_CONFIG=""
S_ROM=""
S_GBASEND=""
S_PORT=""
S_BAUD=""
S_UPLOAD_BAUD=""
S_PAYLOAD=""
S_HANDSHAKE_WAIT=""
S_POLL_INTERVAL=""
if [[ -f $SETTINGS_FILE ]]; then
	while IFS='=' read -r key value; do
		case $key in
			PROFILE) S_PROFILE=$value ;;
			CONFIG) S_CONFIG=$value ;;
			ROM) S_ROM=$value ;;
			GBASEND) S_GBASEND=$value ;;
			PORT) S_PORT=$value ;;
			BAUD) S_BAUD=$value ;;
			UPLOAD_BAUD) S_UPLOAD_BAUD=$value ;;
			PAYLOAD) S_PAYLOAD=$value ;;
			HANDSHAKE_WAIT) S_HANDSHAKE_WAIT=$value ;;
			POLL_INTERVAL) S_POLL_INTERVAL=$value ;;
		esac
	done < "$SETTINGS_FILE"
fi

PROFILE=${APOTRIS_PROFILE:-${S_PROFILE:-$DEFAULT_PROFILE}}
CONFIG_DIR=${FLASHGBLINK_CONFIG:-${S_CONFIG:-}}
ROM=${APOTRIS_ROM:-${S_ROM:-$APOTRIS_ROOT/build-gba/Apotris.gba}}
PORT=${APOTRIS_PORT:-${S_PORT:-}}
BAUD=${APOTRIS_BAUD:-${S_BAUD:-}}
UPLOAD_BAUD=${APOTRIS_UPLOAD_BAUD:-${S_UPLOAD_BAUD:-}}
PAYLOAD=${APOTRIS_PAYLOAD:-${S_PAYLOAD:-}}
handshake_wait=${APOTRIS_HANDSHAKE_WAIT:-${S_HANDSHAKE_WAIT:-120}}
poll_interval=${APOTRIS_POLL_INTERVAL:-${S_POLL_INTERVAL:-1}}

GBASEND=${APOTRIS_GBASEND:-$S_GBASEND}
if [[ -z $GBASEND ]]; then
	if command -v gbasend >/dev/null 2>&1; then
		GBASEND=$(command -v gbasend)
	elif [[ -x $ROOT/../gbasend-rs/gbasend-cli/target/release/gbasend ]]; then
		GBASEND=$ROOT/../gbasend-rs/gbasend-cli/target/release/gbasend
	else
		GBASEND=gbasend
	fi
fi

save_settings() {
	mkdir -p "$(dirname "$SETTINGS_FILE")"
	{
		printf 'PROFILE=%s\n' "$S_PROFILE"
		printf 'CONFIG=%s\n' "$S_CONFIG"
		printf 'ROM=%s\n' "$S_ROM"
		printf 'GBASEND=%s\n' "$S_GBASEND"
		printf 'PORT=%s\n' "$S_PORT"
		printf 'BAUD=%s\n' "$S_BAUD"
		printf 'UPLOAD_BAUD=%s\n' "$S_UPLOAD_BAUD"
		printf 'PAYLOAD=%s\n' "$S_PAYLOAD"
		printf 'HANDSHAKE_WAIT=%s\n' "$S_HANDSHAKE_WAIT"
		printf 'POLL_INTERVAL=%s\n' "$S_POLL_INTERVAL"
	} > "$SETTINGS_FILE"
}

boot=1
watch=0
dry_run=0
save_pending=0
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
		--port)
			[[ $# -ge 2 ]] || { echo "error: --port needs a value" >&2; exit 2; }
			PORT=$2
			S_PORT=$PORT
			save_pending=1
			shift 2
			;;
		--baud)
			[[ $# -ge 2 ]] || { echo "error: --baud needs a value" >&2; exit 2; }
			BAUD=$2
			S_BAUD=$BAUD
			save_pending=1
			shift 2
			;;
		--upload-baud)
			[[ $# -ge 2 ]] || { echo "error: --upload-baud needs a value" >&2; exit 2; }
			UPLOAD_BAUD=$2
			S_UPLOAD_BAUD=$UPLOAD_BAUD
			save_pending=1
			shift 2
			;;
		--payload)
			[[ $# -ge 2 ]] || { echo "error: --payload needs a path" >&2; exit 2; }
			PAYLOAD=$2
			S_PAYLOAD=$PAYLOAD
			save_pending=1
			shift 2
			;;
		--config)
			[[ $# -ge 2 ]] || { echo "error: --config needs a directory" >&2; exit 2; }
			CONFIG_DIR=$2
			S_CONFIG=$CONFIG_DIR
			save_pending=1
			shift 2
			;;
		--handshake-wait)
			[[ $# -ge 2 ]] || { echo "error: --handshake-wait needs seconds" >&2; exit 2; }
			handshake_wait=$2
			S_HANDSHAKE_WAIT=$handshake_wait
			save_pending=1
			shift 2
			;;
		--interval)
			[[ $# -ge 2 ]] || { echo "error: --interval needs seconds" >&2; exit 2; }
			poll_interval=$2
			S_POLL_INTERVAL=$poll_interval
			save_pending=1
			shift 2
			;;
		--verbose)
			common_args+=(--verbose)
			shift
			;;
		--rom)
			[[ $# -ge 2 ]] || { echo "error: --rom needs a path" >&2; exit 2; }
			ROM=$2
			S_ROM=$ROM
			save_pending=1
			shift 2
			;;
		--profile)
			[[ $# -ge 2 ]] || { echo "error: --profile needs a name" >&2; exit 2; }
			PROFILE=$2
			S_PROFILE=$PROFILE
			save_pending=1
			shift 2
			;;
		--gbasend)
			[[ $# -ge 2 ]] || { echo "error: --gbasend needs a path" >&2; exit 2; }
			GBASEND=$2
			S_GBASEND=$GBASEND
			save_pending=1
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
if [[ -n $CONFIG_DIR ]]; then
	command+=(--config "$CONFIG_DIR")
fi
if [[ -n $PORT ]]; then
	command+=(--port "$PORT")
fi
if [[ -n $BAUD ]]; then
	command+=(--baud "$BAUD")
fi
if [[ -n $UPLOAD_BAUD ]]; then
	command+=(--upload-baud "$UPLOAD_BAUD")
fi
if [[ -n $PAYLOAD ]]; then
	command+=(--payload "$PAYLOAD")
fi
if ((boot)); then
	command+=(--boot)
fi
command+=("${common_args[@]}")
command+=("${flash_args[@]}")

upload_command=("$PYTHON" "$ROOT/flash.py" upload
	--gbasend "$GBASEND" --handshake-wait "$handshake_wait")
if [[ -n $PORT ]]; then
	upload_command+=(--port "$PORT")
fi
if [[ -n $UPLOAD_BAUD ]]; then
	upload_command+=(--upload-baud "$UPLOAD_BAUD")
fi
if [[ -n $PAYLOAD ]]; then
	upload_command+=(--payload "$PAYLOAD")
fi
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

timestamp() {
	date '+%Y-%m-%d %H:%M:%S'
}

format_elapsed() {
	local seconds=$1
	if ((seconds >= 3600)); then
		printf '%dh %02dm %02ds' \
			$((seconds / 3600)) $((seconds % 3600 / 60)) $((seconds % 60))
	elif ((seconds >= 60)); then
		printf '%dm %02ds' $((seconds / 60)) $((seconds % 60))
	else
		printf '%ds' "$seconds"
	fi
}

# Run the flash command and report when it finished and how long it took.
flash_once() {
	local started finished elapsed status
	printf '\n== %s flashing %s ==\n' "$(timestamp)" "$(basename "$ROM")"
	started=$(date +%s)
	if "${command[@]}"; then
		status=0
	else
		status=$?
	fi
	finished=$(date +%s)
	elapsed=$(format_elapsed $((finished - started)))
	if ((status == 0)); then
		printf '== %s flash finished in %s ==\n' "$(timestamp)" "$elapsed"
	else
		printf '== %s flash failed after %s ==\n' "$(timestamp)" "$elapsed" >&2
	fi
	return "$status"
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
						if ! flash_once; then
							if ((!boot)); then
								printf 'error: flash failed; restart the watcher\n' >&2
								return 1
							fi
							printf 'flash failed; trying a fresh payload upload...\n' >&2
							if ! upload_payload; then
								printf 'error: upload failed; restart the watcher\n' >&2
								return 1
							fi
							if ! flash_once; then
								printf 'error: flash failed after upload; restart the watcher\n' >&2
								return 1
							fi
						fi
						if ((boot)); then
							needs_upload=1
						fi
						if ((save_pending)); then
							save_settings
							save_pending=0
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
elif ((save_pending)); then
	if flash_once; then
		save_settings
	else
		status=$?
		exit "$status"
	fi
else
	flash_once
fi
