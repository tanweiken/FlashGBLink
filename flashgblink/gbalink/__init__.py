"""Transport and write-plan helpers shared by the CLI and the FlashGBX plugin."""

from .errors import ProtocolError, TransferError, UnsupportedCommand
from .plan import (ADDR_ABS, ADDR_PA, ADDR_SA, PLAN_MAX_COMMANDS, PlanError,
                   VAL_BS, VAL_CONST, VAL_PD, WAIT_ABS, WAIT_NONE, WAIT_PD,
                   WAIT_SA, encode_write_plan, prepare_write,
                   single_write_waits)
from .transport import (BLOCK_BYTES, CAP_READ_CHECK, CAPS_VERSION,
                        GBASEND_PID, GBASEND_VID, HDR_MAGIC, OP_ABORT,
                        OP_BOOT, OP_CAPS, OP_CRC32, OP_CRC_GET, OP_DONE,
                        OP_PROGRESS, OP_READ, OP_READ8, OP_READ8_CHECK,
                        OP_READ_CHECK, OP_SET_PLAN, OP_WRITE8,
                        OP_WRITE_BLOCK, OP_WRITE_SEQ, PAYLOAD, PING,
                        READ_CHECK_ATTEMPTS, RESYNC_WORDS, ST_BAD, ST_ECHO,
                        ST_OK, ST_TIMEOUT, Link, _check_status, _hex,
                        _pack_words, done,
                        find_port, progress, read_bytes, read_region,
                        write_block)

__all__ = [
    "ADDR_ABS", "ADDR_PA", "ADDR_SA", "BLOCK_BYTES", "CAP_READ_CHECK",
    "CAPS_VERSION", "GBASEND_PID", "GBASEND_VID", "HDR_MAGIC", "Link",
    "OP_ABORT", "OP_BOOT", "OP_CAPS", "OP_CRC32", "OP_CRC_GET", "OP_DONE",
    "OP_PROGRESS",
    "OP_READ", "OP_READ8", "OP_READ8_CHECK", "OP_READ_CHECK", "OP_SET_PLAN",
    "OP_WRITE8", "OP_WRITE_BLOCK", "OP_WRITE_SEQ", "PAYLOAD", "PING",
    "PLAN_MAX_COMMANDS", "PlanError", "ProtocolError", "READ_CHECK_ATTEMPTS",
    "RESYNC_WORDS", "ST_BAD", "ST_ECHO", "ST_OK", "ST_TIMEOUT",
    "TransferError",
    "UnsupportedCommand", "VAL_BS", "VAL_CONST", "VAL_PD", "WAIT_ABS",
    "WAIT_NONE", "WAIT_PD", "WAIT_SA", "_check_status", "_hex", "_pack_words",
    "encode_write_plan", "find_port", "prepare_write", "done", "progress",
    "read_bytes", "read_region", "single_write_waits", "write_block",
]
