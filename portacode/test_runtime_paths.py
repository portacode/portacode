from __future__ import annotations

import os
from unittest import TestCase
from unittest.mock import patch

from portacode.utils.runtime_paths import expand_runtime_path


class RuntimePathTests(TestCase):
    @patch.dict(os.environ, {"PORTACODE_DEFAULT_RUNTIME_USER": "meena"}, clear=False)
    @patch("pwd.getpwnam")
    def test_expand_runtime_path_uses_runtime_user_home_for_tilde(self, mock_getpwnam):
        mock_getpwnam.return_value.pw_dir = "/home/meena"

        expanded = expand_runtime_path("~/.openclaw")

        self.assertEqual(expanded, "/home/meena/.openclaw")
