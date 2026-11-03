"""Exceptions shared by the link transport and the cartridge plan encoder."""


class ProtocolError(RuntimeError):
    pass


class UnsupportedCommand(ProtocolError):
    """The running payload does not implement the command that was sent.

    A payload from before an opcode was added rejects it and keeps answering,
    so the link is still in sync; the caller can fall back to an older path.
    """


class TransferError(ProtocolError):
    """A transfer ended short; the word framing is now suspect."""

    def __init__(self, message, missing=0):
        super().__init__(message)
        self.missing = missing
