"""FlashGBLink: use the GBA link-port payload as a FlashGBX device."""

import sys


def install():
    """Register the device and, in GUI mode, the Connect upload hook."""
    import FlashGBX.app as app

    from . import device as device_module

    if device_module not in app.HW_DEVICES:
        app.HW_DEVICES.append(device_module)

    if "FlashGBX.FlashGBX_GUI" in sys.modules:
        try:
            from .gui import install_gui_hook
            install_gui_hook()
        except Exception as error:
            try:
                from FlashGBX.Logging import dprint
                dprint("FlashGBLink: GUI hook not installed:", error)
            except Exception:
                pass


def __getattr__(name):
    if name == "GbxDevice":
        from .device import GbxDevice
        return GbxDevice
    raise AttributeError(name)


__all__ = ["GbxDevice", "install"]
