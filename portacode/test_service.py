from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from portacode.service import _OpenRCService, _SystemdUserService, _build_connect_command


class ServiceCommandTests(TestCase):
    @patch.dict(
        os.environ,
        {
            "PORTACODE_PROJECT_PATH_2": "$HOME/workspace",
            "PORTACODE_PROJECT_PATH_1": "~/.openclaw",
        },
        clear=False,
    )
    def test_build_connect_command_includes_project_paths_in_order(self):
        command = _build_connect_command("/opt/portacode-venv/bin/python")

        self.assertIn("--project-path '~/.openclaw'", command)
        self.assertIn("--project-path '$HOME/workspace'", command)
        self.assertLess(command.index("~/.openclaw"), command.index("$HOME/workspace"))

    @patch.dict(
        os.environ,
        {
            "PORTACODE_PROJECT_PATH_2": "$HOME/.openclaw/workspace",
            "PORTACODE_PROJECT_PATH_1": "$HOME/.openclaw",
            "PORTACODE_DEFAULT_RUNTIME_USER": "user",
            "PORTACODE_XDG_DATA_HOME": "/home/user/.local/share",
            "USER": "root",
        },
        clear=False,
    )
    @patch("portacode.service.os.geteuid", return_value=0)
    @patch("portacode.service._SystemdUserService._run_checked")
    def test_systemd_system_unit_preserves_project_paths(self, mock_run_checked, _mock_geteuid):
        with TemporaryDirectory() as tmpdir:
            service = _SystemdUserService(system_mode=True)
            service.service_path = Path(tmpdir) / "portacode.service"
            service.home = Path("/root")
            service.python = "/opt/portacode-venv/bin/python"

            service.install()

            unit_text = service.service_path.read_text(encoding="utf-8")
            self.assertIn("ExecStart=/opt/portacode-venv/bin/python -m portacode connect --non-interactive", unit_text)
            self.assertIn("--project-path '$$HOME/.openclaw'", unit_text)
            self.assertIn("--project-path '$$HOME/.openclaw/workspace'", unit_text)
            self.assertLess(unit_text.index("$$HOME/.openclaw"), unit_text.index("$$HOME/.openclaw/workspace"))
            self.assertEqual(mock_run_checked.call_args_list[0].args, ("daemon-reload",))
            self.assertEqual(mock_run_checked.call_args_list[1].args, ("enable", "--now", "portacode"))

    @patch.dict(
        os.environ,
        {
            "PORTACODE_PROJECT_PATH_2": "$HOME/.openclaw/workspace",
            "PORTACODE_PROJECT_PATH_1": "$HOME/.openclaw",
            "PORTACODE_DEFAULT_RUNTIME_USER": "user",
            "PORTACODE_XDG_DATA_HOME": "/home/user/.local/share",
            "USER": "root",
        },
        clear=False,
    )
    @patch("portacode.service.os.geteuid", return_value=0)
    def test_openrc_wrapper_preserves_project_paths(self, _mock_geteuid):
        with TemporaryDirectory() as tmpdir:
            service = _OpenRCService()
            service.wrapper_path = Path(tmpdir) / "connect_service.sh"
            service.log_path = Path(tmpdir) / "connect.log"
            service.home = Path("/root")
            service.python = "/opt/portacode-venv/bin/python"

            service._write_wrapper_script()

            script_text = service.wrapper_path.read_text(encoding="utf-8")
            self.assertIn('/opt/portacode-venv/bin/python -m portacode connect --non-interactive', script_text)
            self.assertIn("--project-path '$HOME/.openclaw'", script_text)
            self.assertIn("--project-path '$HOME/.openclaw/workspace'", script_text)
            self.assertLess(script_text.index("$HOME/.openclaw"), script_text.index("$HOME/.openclaw/workspace"))
