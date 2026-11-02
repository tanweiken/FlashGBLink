"""Exceptions shared by the link transport and the cartridge plan encoder."""


class ProtocolError(RuntimeError):
    pass


class TransferError(ProtocolError):
    """A transfer ended short; the word framing is now suspect."""

    def __init__(self, message, missing=0):
        super().__init__(message)
        self.missing = missing
