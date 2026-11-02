"""FlashGBLink: use the GBA link-port payload as a FlashGBX device."""

import ast
from pathlib import Path


def _plugin_version():
    """Read PLUGIN_VERSION from the sibling plugin file.

    FlashGBX reads that constant from ``flashgblink_plugin.py`` for its
    plugin list, so keeping it there gives the release one version source
    for both the plugin metadata and the firmware version shown by the
    device.
    """
    path = Path(__file__).resolve().parent.parent / "flashgblink_plugin.py"
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return "unknown"
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name)
                   and target.id == "PLUGIN_VERSION"
                   for target in node.targets):
            continue
        try:
            value = ast.literal_eval(node.value)
        except (ValueError, TypeError):
            continue
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "unknown"


__version__ = _plugin_version()


def install():
    """Register the device and, in GUI mode, the Connect upload hook."""
    import FlashGBX.app as app

    from . import device as device_module

    if device_module not in app.HW_DEVICES:
        app.HW_DEVICES.append(device_module)

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


__all__ = ["GbxDevice", "install", "__version__"]
