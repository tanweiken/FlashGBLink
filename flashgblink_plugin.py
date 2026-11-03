# -*- coding: utf-8 -*-
"""FlashGBX plugin: expose the GBA link-port payload as a device.

Copy this file and the ``flashgblink`` folder into FlashGBX's plugin
directory, or point ``FLASHGBLINK_ROOT`` at this repository.  Requires
FlashGBX 5.2.1 or newer (the plugin system).
"""

import os
import sys

PLUGIN_NAME = "FlashGBLink"
PLUGIN_DESCRIPTION = (
    "Read and write GBA cartridges through the GBA link port and an "
    "RP2040 Pico running the gbasend firmware with the flasher payload.")
PLUGIN_VERSION = "0.3.0b4"

_ROOT = os.environ.get("FLASHGBLINK_ROOT") or os.path.dirname(
    os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def plugin_entry():
    from flashgblink import install
    install()
