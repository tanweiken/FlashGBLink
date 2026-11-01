"""Encode a FlashGBX write template for the GBA payload.

The payload holds the profile's command template, so a new cartridge does not
need a payload rebuild.  Four words per command are installed with
``OP_SET_PLAN``: address, value, wait address and expected/mask.  The payload
substitutes the buffer size and the program data when it executes the plan.
"""

from .errors import ProtocolError
from .transport import BLOCK_BYTES

# Write-plan command words share the payload's one-byte header opcode space.
PLAN_MAX_COMMANDS = 12
ADDR_ABS, ADDR_SA, ADDR_PA = 0, 1, 2
VAL_CONST, VAL_BS, VAL_PD = 0, 1, 2
WAIT_NONE, WAIT_ABS, WAIT_SA, WAIT_PD = 0, 1, 2, 3


class PlanError(ProtocolError):
    pass


def _addr_token(token):
    if isinstance(token, int):
        return ADDR_ABS, token & 0x3FFFFFFF
    for prefix, kind in (("SA+", ADDR_SA), ("PA+", ADDR_PA)):
        if isinstance(token, str) and token.startswith(prefix):
            return kind, int(token[len(prefix):], 0) & 0x3FFFFFFF
    if token == "SA":
        return ADDR_SA, 0
    if token == "PA":
        return ADDR_PA, 0
    raise PlanError("unknown address token %r" % (token,))


def _val_token(token):
    if isinstance(token, int):
        return VAL_CONST, token & 0xFFFF
    if token == "BS":
        return VAL_BS, 0
    if token == "PD":
        return VAL_PD, 0
    raise PlanError("unknown data token %r" % (token,))


def _wait_word(row):
    if not row or row[0] is None:
        return 0, 0, 0, 0
    kind, address = _addr_token(row[0])
    # Numeric wait addresses in the profile are offsets from the operation
    # address (0 for the word just programmed, 2 for the second lane), not
    # absolute cartridge addresses.
    wait_kind = WAIT_SA
    expected = row[1]
    if expected == "PD":
        if wait_kind != WAIT_SA:
            raise PlanError("wait-for PD must read at SA or PA")
        wait_kind = WAIT_PD
        expected = 0
    elif not isinstance(expected, int):
        raise PlanError("unknown wait-for value %r" % (expected,))
    mask = row[2] if len(row) > 2 and row[2] is not None else 0xFFFF
    if not isinstance(mask, int):
        raise PlanError("unknown wait-for mask %r" % (mask,))
    return wait_kind, address, expected, mask


def single_write_waits(commands, waits, command_set=""):
    """Wait rows for single-word programming, with a status fallback.

    Several AMD profiles leave the wait table empty because FlashGBX's
    firmware polls the flash itself.  This payload needs the condition spelled
    out, so an absent wait becomes Intel SR.7 or AMD data polling.
    """
    waits = [list(row) for row in waits]
    waits.extend([[None, None, None]] * (len(commands) - len(waits)))
    pd_index = next((index for index, row in enumerate(commands)
                     if len(row) > 1 and row[1] == "PD"), None)
    if pd_index is None:
        return waits
    if any(row and row[0] is not None for row in waits[pd_index:]):
        return waits
    if str(command_set).upper() == "AMD":
        waits[pd_index] = ["PA", "PD", 0xFFFF]
    else:
        waits[pd_index] = ["SA", 0x80, 0xFFFF]
    return waits


def encode_write_plan(commands, buffer_size=None, command_set="",
                      name="profile"):
    """Encode a profile's write template for the payload.

    ``commands`` is the FlashGBX ``commands`` mapping (``buffer_write``,
    ``single_write`` and their ``*_wait_for`` tables).  Returns
    ``(chunk_bytes, words)``.  ``words`` is four words per command: address,
    value, wait address and expected/mask.  The first PD command is where the
    payload streams the data words.
    """
    if commands.get("buffer_write") and buffer_size and buffer_size > 2:
        key = "buffer_write"
        rows = commands[key]
        waits = commands.get(key + "_wait_for", [])
    elif commands.get("single_write"):
        key = "single_write"
        rows = commands[key]
        waits = single_write_waits(rows, commands.get(key + "_wait_for", []),
                                   command_set)
        buffer_size = 2
    else:
        raise PlanError("profile %r has no buffer or single write command"
                        % name)
    if not buffer_size or buffer_size % 2:
        raise PlanError("write chunk must be an even byte count")
    if buffer_size > BLOCK_BYTES:
        raise PlanError("write chunk %d is larger than %d bytes"
                        % (buffer_size, BLOCK_BYTES))

    words = []
    data_markers = 0
    for index, row in enumerate(rows):
        if len(row) < 2:
            raise PlanError("malformed %s command %r" % (key, row))
        addr_kind, address = _addr_token(row[0])
        val_kind, value = _val_token(row[1])
        if val_kind == VAL_PD:
            data_markers += 1
        wait_kind, wait_address, expected, mask = _wait_word(
            waits[index] if index < len(waits) else None)
        words.extend([
            (addr_kind << 30) | address,
            (val_kind << 30) | value,
            (wait_kind << 30) | wait_address,
            ((expected & 0xFFFF) << 16) | (mask & 0xFFFF),
        ])
    if data_markers != 1:
        raise PlanError("%s needs exactly one PD marker, found %d"
                        % (key, data_markers))
    if len(words) // 4 > PLAN_MAX_COMMANDS:
        raise PlanError("%s has %d commands; the payload holds %d"
                        % (key, len(words) // 4, PLAN_MAX_COMMANDS))
    return buffer_size, words


def prepare_write(link, commands, buffer_size=None, command_set="",
                  name="profile"):
    """Install a profile's write plan and return the payload block size."""
    chunk_bytes, words = encode_write_plan(
        commands, buffer_size, command_set=command_set, name=name)
    link.set_write_plan(chunk_bytes, words)
    # Keep the transport frame a multiple of the chip's write window.
    block_bytes = (BLOCK_BYTES // chunk_bytes) * chunk_bytes
    return max(block_bytes, chunk_bytes), chunk_bytes
