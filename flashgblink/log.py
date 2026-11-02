"""Log plugin events to FlashGBX's debug log and a plugin log file."""

from pathlib import Path


def log(message):
    try:
        from FlashGBX.Logging import dprint
        dprint(message)
    except Exception:
        pass
    try:
        from FlashGBX.app import AppContext
        if AppContext.CONFIG_PATH:
            path = Path(AppContext.CONFIG_PATH) / "flashgblink.log"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(str(message) + "\n")
    except Exception:
        pass
