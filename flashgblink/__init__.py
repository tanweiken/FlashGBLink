"""FlashGBLink: use the GBA link-port payload as a FlashGBX device."""

from .device import GbxDevice


def install():
    """Append this backend to FlashGBX's hardware list (plugin entry point)."""
    import FlashGBX.app as app
    import flashgblink.device as device_module

    if device_module not in app.HW_DEVICES:
        app.HW_DEVICES.append(device_module)


__all__ = ["GbxDevice", "install"]
