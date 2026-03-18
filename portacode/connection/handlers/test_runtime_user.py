from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from portacode.connection.handlers.runtime_user import (
    wrap_shell_command,
    wrap_argv_for_user,
    write_text_preserve_metadata,
)


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

    @patch("portacode.connection.handlers.runtime_user.get_runtime_user_home", return_value="/home/meena")
    @patch("portacode.connection.handlers.runtime_user.pwd.getpwnam")
    @patch("portacode.connection.handlers.runtime_user.os.geteuid", return_value=0)
    def test_wrap_shell_command_sources_bashrc_for_switched_user(self, _mock_euid, mock_getpwnam, _mock_home):
        mock_getpwnam.return_value = type("Pw", (), {"pw_shell": "/bin/bash"})()

        wrapped = wrap_shell_command("openclaw status", "meena")

        self.assertIn("sudo -H -i -u meena -- /bin/bash -lc", wrapped)
        self.assertIn("openclaw status", wrapped)

    @patch("portacode.connection.handlers.runtime_user.get_runtime_user_home", return_value="/home/meena")
    @patch("portacode.connection.handlers.runtime_user.pwd.getpwnam")
    @patch("portacode.connection.handlers.runtime_user.os.geteuid", return_value=0)
    def test_wrap_shell_command_preserves_requested_env_names(self, _mock_euid, mock_getpwnam, _mock_home):
        mock_getpwnam.return_value = type("Pw", (), {"pw_shell": "/bin/bash"})()

        wrapped = wrap_shell_command(
            "env | grep TELEGRAM",
            "meena",
            preserve_env_names=["TELEGRAM_BOT_TOKEN", "PORTACODE_DEBUG_INPUT"],
        )

        self.assertIn("--preserve-env=TELEGRAM_BOT_TOKEN,PORTACODE_DEBUG_INPUT", wrapped)

    @patch("portacode.connection.handlers.runtime_user.os.geteuid", return_value=0)
    @patch("portacode.connection.handlers.runtime_user.pwd.getpwnam")
    def test_wrap_argv_for_user_preserves_login_mode_and_explicitly_reenters_cwd(self, mock_getpwnam, _mock_euid):
        mock_getpwnam.return_value = type("Pw", (), {"pw_shell": "/bin/bash"})()

        wrapped = wrap_argv_for_user(["/bin/bash", "--login"], "meena", cwd="/home/meena/.openclaw")

        self.assertEqual(wrapped[:5], ["sudo", "-H", "-i", "-u", "meena"])
        self.assertEqual(wrapped[5:8], ["--", "/bin/bash", "-lc"])
        self.assertIn("cd /home/meena/.openclaw && exec /bin/bash --login", wrapped[8])

    @patch("portacode.connection.handlers.runtime_user.os.geteuid", return_value=0)
    @patch("portacode.connection.handlers.runtime_user.pwd.getpwnam")
    def test_wrap_argv_for_user_uses_shell_for_non_shell_commands(self, mock_getpwnam, _mock_euid):
        mock_getpwnam.return_value = type("Pw", (), {"pw_shell": "/bin/bash"})()

        wrapped = wrap_argv_for_user(["git", "add", "-A", "--", "README.md"], "meena", cwd="/repo")

        self.assertEqual(wrapped[:5], ["sudo", "-H", "-i", "-u", "meena"])
        self.assertEqual(wrapped[5:8], ["--", "/bin/bash", "-lc"])
        self.assertIn("cd /repo && exec git add -A -- README.md", wrapped[8])
