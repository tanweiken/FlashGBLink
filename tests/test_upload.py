"""Tests for payload/gbasend discovery and the upload command."""

import os
import tempfile
import unittest
from pathlib import Path

from flashgblink import upload


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {}
        for key in ("FLASHGBLINK_PAYLOAD", "FLASHGBLINK_GBASEND"):
            self.env[key] = os.environ.get(key)
        self.root = Path(self.tmp.name)

    def tearDown(self):
        for key, value in self.env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_payload_environment_override(self):
        payload = self.root / "flasher.gba"
        payload.write_bytes(b"payload")
        os.environ["FLASHGBLINK_PAYLOAD"] = str(payload)
        self.assertEqual(upload.find_payload(), payload)

    def test_gbasend_environment_override(self):
        gbasend = self.root / "gbasend"
        gbasend.write_bytes(b"binary")
        os.environ["FLASHGBLINK_GBASEND"] = str(gbasend)
        self.assertEqual(upload.find_gbasend(), gbasend)

    def test_upload_command(self):
        command = upload.upload_command(
            Path("/bin/gbasend"), "/dev/fake", Path("/tmp/flasher.gba"),
            baud=256000, handshake_wait=30)
        self.assertEqual(command, [
            "/bin/gbasend", "-p", "/dev/fake", "-b", "256000",
            "-m", "--handshake-wait", "30", "/tmp/flasher.gba"])

    def test_baud_and_handshake_defaults(self):
        command = upload.upload_command(
            Path("gbasend"), "COM3", Path("flasher.gba"))
        self.assertIn("256000", command)
        self.assertIn("30", command)


if __name__ == "__main__":
    unittest.main()
