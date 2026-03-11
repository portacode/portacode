from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from portacode.connection.handlers.runtime_user import write_text_preserve_metadata


class RuntimeUserTests(TestCase):
    def test_write_text_preserves_existing_mode(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "example.txt"
            target.write_text("before", encoding="utf-8")
            target.chmod(0o640)

            bytes_written = write_text_preserve_metadata(target, "after")

            self.assertEqual(bytes_written, len("after".encode("utf-8")))
            self.assertEqual(target.read_text(encoding="utf-8"), "after")
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)

    @patch("portacode.connection.handlers.runtime_user.chown_path_if_possible")
    def test_write_text_assigns_owner_for_new_file_when_requested(self, mock_chown):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "nested" / "created.txt"

            write_text_preserve_metadata(target, "hello", create_user="appuser")

            self.assertTrue(target.exists())
            self.assertEqual(target.read_text(encoding="utf-8"), "hello")
            mock_chown.assert_any_call(target.parent, "appuser")
            mock_chown.assert_any_call(target, "appuser")
