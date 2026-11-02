"""FlashGBX GUI integration: upload the payload when Connect cannot see it."""

import time

from FlashGBX.Logging import dprint
from FlashGBX.pyside import QtCore, QtWidgets

from . import upload
from .log import log as _log


def _show_error(parent, message):
    dprint("FlashGBLink:", message)
    QtWidgets.QMessageBox.critical(parent, "FlashGBLink", message)


class UploadDialog:
    """Cancellable, event-loop-driven gbasend upload."""

    def __init__(self, parent, port, gbasend, payload):
        self.success = False
        self.cancelled = False
        self._done = False
        self.dialog = QtWidgets.QProgressDialog(
            "Hold START+SELECT and power on the GBA.", "Cancel", 0, 0, parent)
        self.dialog.setWindowTitle("FlashGBLink payload")
        self.dialog.setMinimumDuration(0)
        self.dialog.setAutoClose(False)
        self.dialog.setAutoReset(False)
        self.dialog.setWindowModality(QtCore.Qt.WindowModal)
        self.loop = QtCore.QEventLoop()
        self.process = QtCore.QProcess(parent)
        self.process.setProcessChannelMode(QtCore.QProcess.MergedChannels)
        self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._error)
        self.dialog.canceled.connect(self._cancel)
        self.timer = QtCore.QTimer(parent)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._timeout)
        command = upload.upload_command(gbasend, port, payload)
        self.command = command
        self.process.setProgram(command[0])
        self.process.setArguments(command[1:])

    def _finish(self, success):
        if self._done:
            return
        self._done = True
        self.success = success
        self.timer.stop()
        output = bytes(self.process.readAllStandardOutput()).decode(
            "utf-8", "replace").strip()
        if output:
            dprint("FlashGBLink gbasend output:\n" + output)
        if not success:
            dprint("FlashGBLink upload command:", self.command)
        self.loop.quit()

    def _finished(self, code, status):
        self._finish(status == QtCore.QProcess.NormalExit and code == 0)

    def _error(self, error):
        dprint("FlashGBLink gbasend process error:", error)
        self._finish(False)

    def _timeout(self):
        dprint("FlashGBLink upload timed out:", self.command)
        self.process.kill()
        self._finish(False)

    def _cancel(self):
        self.cancelled = True
        dprint("FlashGBLink upload cancelled")
        self.process.kill()
        self._finish(False)

    def run(self):
        dprint("FlashGBLink upload command:", self.command)
        self.dialog.show()
        self.process.start()
        self.timer.start(60_000)
        self.loop.exec()
        self.dialog.close()
        return self.success


def _maybe_upload(parent, port=None):
    pico_port = upload.find_link_port()
    if not pico_port:
        _log("FlashGBLink: no Pico serial port found")
        return False
    _log("FlashGBLink: Connect check on %s" % pico_port)
    if port and port != pico_port:
        return False
    if upload.payload_running(pico_port):
        return False

    gbasend = upload.find_gbasend()
    if gbasend is None:
        _show_error(
            parent,
            "gbasend was not found, so FlashGBLink cannot upload the payload.\n\n"
            "Install/build gbasend-rs, put gbasend on PATH, or set "
            "FLASHGBLINK_GBASEND to its path.")
        return False
    payload = upload.find_payload()
    if payload is None:
        _show_error(
            parent,
            "The flasher payload was not found. Build it with "
            "`make -C payload`, or set FLASHGBLINK_PAYLOAD to flasher.gba.")
        return False

    dialog = UploadDialog(parent, pico_port, gbasend, payload)
    if dialog.run():
        _log("FlashGBLink: payload uploaded on %s" % pico_port)
        # Do not open the port again for a readiness ping: macOS needs a
        # moment to release it, and a second open makes FlashGBX's discovery
        # lose the race.  Let the payload settle instead.
        time.sleep(1.0)
        return True
    if not dialog.cancelled:
        _show_error(
            parent,
            "The payload upload did not complete. Check that gbasend and the "
            "Pico are working, then try Connect again. Details are in the "
            "FlashGBX debug log.")
    _log("FlashGBLink: payload upload failed or was cancelled")
    return False


def install_gui_hook():
    """Wrap FlashGBX_GUI.FindDevices so Connect can upload the payload."""
    from FlashGBX import FlashGBX_GUI

    cls = FlashGBX_GUI.FlashGBX_GUI
    if getattr(cls, "_flashgblink_connect_hook", False):
        return
    original = cls.FindDevices

    def FindDevices(self, connectToFirst=False, port=None, mode=None,
                    firstRun=False):
        uploaded = False
        if not firstRun and getattr(self, "CONN", None) is None:
            if not getattr(self, "_flashgblink_upload_busy", False):
                self._flashgblink_upload_busy = True
                try:
                    uploaded = _maybe_upload(self, port)
                except Exception as error:
                    _show_error(
                        self, "FlashGBLink could not prepare the payload:\n%s"
                        % error)
                finally:
                    self._flashgblink_upload_busy = False

        if uploaded:
            try:
                from serial.tools import list_ports
                _log("FlashGBLink: ports before discovery: %r"
                     % [(p.device, p.vid, p.pid)
                        for p in list_ports.comports()])
            except Exception:
                pass
            # Probe quietly (firstRun suppresses the no-device dialog), then
            # retry once after the port has settled.
            original(self, connectToFirst=connectToFirst, port=port,
                     mode=mode, firstRun=True)
            if not getattr(self, "DEVICES", None):
                time.sleep(1.0)
                result = original(self, connectToFirst=connectToFirst,
                                  port=port, mode=mode, firstRun=firstRun)
            else:
                result = True
        else:
            result = original(self, connectToFirst=connectToFirst, port=port,
                              mode=mode, firstRun=firstRun)
        _log("FlashGBLink: discovery returned %d device(s)"
             % len(getattr(self, "DEVICES", {})))
        return result

    cls.FindDevices = FindDevices
    cls._flashgblink_connect_hook = True
    _log("FlashGBLink: Connect upload hook installed")
