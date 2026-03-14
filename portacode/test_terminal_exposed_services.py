import asyncio
from unittest import TestCase
from unittest.mock import call, patch

from portacode.connection.terminal import (
    DEFAULT_ENV_PATH,
    EXPOSED_SERVICES_ENV_PATH,
    EXPOSED_SERVICES_MISSING_SIGNATURE,
    EXPOSED_SERVICES_PROFILE_PATH,
    GLOBAL_SHELL_HOOK_PATHS,
    OPENRC_ENV_PATH,
    SYSTEMD_MANAGER_DROPIN_PATH,
    SYSTEM_ENV_D_PATH,
    SYSTEM_ENV_PATH,
    TerminalManager,
)


class TerminalExposedServicesTests(TestCase):
    @patch("portacode.connection.terminal.run")
    @patch("portacode.connection.terminal.write_text")
    @patch("portacode.connection.terminal.read_text", side_effect=FileNotFoundError())
    def test_sync_exposed_services_compat_files_writes_local_derived_files(
        self,
        _mock_read_text,
        mock_write_text,
        mock_run,
    ):
        manager = TerminalManager.__new__(TerminalManager)
        services = [
            {"hostname": "123.example.com", "url": "https://123.example.com", "port": 443},
            {"hostname": "1_123.example.com", "url": "https://1_123.example.com", "port": 12321},
        ]
        mock_run.return_value.returncode = 0

        manager._sync_exposed_services_compat_files(services)

        written_paths = [args[0] for args, _kwargs in mock_write_text.call_args_list]
        self.assertEqual(
            written_paths,
            [
                EXPOSED_SERVICES_ENV_PATH,
                EXPOSED_SERVICES_PROFILE_PATH,
                *GLOBAL_SHELL_HOOK_PATHS,
                SYSTEM_ENV_PATH,
                SYSTEM_ENV_D_PATH,
                DEFAULT_ENV_PATH,
                SYSTEMD_MANAGER_DROPIN_PATH,
                OPENRC_ENV_PATH,
            ],
        )
        self.assertEqual(mock_write_text.call_count, 11)
        self.assertEqual(mock_run.call_count, 2)
        self.assertEqual(
            mock_run.call_args_list,
            [
                call(
                    [
                        "sh",
                        "-lc",
                        "if command -v systemctl >/dev/null 2>&1; then systemctl daemon-reexec >/dev/null 2>&1 || true; fi",
                    ]
                ),
                call(
                    [
                        "sh",
                        "-lc",
                        "if command -v env-update >/dev/null 2>&1; then env-update >/dev/null 2>&1 || true; fi",
                    ]
                ),
            ],
        )

    @patch("portacode.connection.terminal.apply_turnkey_webmin_proxy_config")
    @patch("portacode.connection.terminal.asyncio.sleep", side_effect=asyncio.CancelledError)
    def test_watch_exposed_services_skips_compat_sync_when_json_missing(
        self,
        _mock_sleep,
        mock_apply_webmin,
    ):
        manager = TerminalManager.__new__(TerminalManager)
        manager._last_exposed_services_signature = "__unset__"
        manager._client_session_manager = type(
            "ClientSessions",
            (),
            {"has_interested_clients": staticmethod(lambda: False)},
        )()
        manager._read_exposed_services_snapshot = lambda: {
            "signature": EXPOSED_SERVICES_MISSING_SIGNATURE,
            "services": [],
        }
        manager._apply_exposed_services_env_to_process = lambda services: None
        manager._sync_exposed_services_compat_files = lambda services: (_ for _ in ()).throw(
            AssertionError("compat sync should not run without canonical JSON")
        )

        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(manager._watch_exposed_services())

        self.assertEqual(manager._last_exposed_services_signature, EXPOSED_SERVICES_MISSING_SIGNATURE)
        mock_apply_webmin.assert_called_once_with([])
