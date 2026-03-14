from __future__ import annotations

import os
from unittest import TestCase
from unittest.mock import patch

from portacode.service import _build_connect_command


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
