from unittest import TestCase
from unittest.mock import MagicMock, patch

from portacode.connection.handlers.proxmox_infra import (
    RemoveProxmoxContainerHandler,
    _build_bootstrap_steps,
    _enforce_service_venv_execstart,
    _get_provisioning_user_info,
    _resolve_user_data_dir,
    _sanitize_project_paths,
)


class ProxmoxInfraHandlerTests(TestCase):
    @patch("portacode.connection.handlers.proxmox_infra._run_pct_check")
    def test_resolve_user_data_dir_uses_passwd_lookup_not_login_shell(self, mock_run_pct_check):
        mock_run_pct_check.return_value = {"stdout": "/root", "stderr": "", "returncode": 0}

        path = _resolve_user_data_dir(145, "root")

        self.assertEqual(path, "/root/.local/share")
        called_command = mock_run_pct_check.call_args[0][1]
        self.assertIn("getent passwd", called_command)
        self.assertNotIn("su - root", called_command)

    def test_get_provisioning_user_info_defaults_to_root_with_generated_password(self):
        user, password, ssh_key = _get_provisioning_user_info({})

        self.assertEqual(user, "root")
        self.assertTrue(password)
        self.assertEqual(ssh_key, "")

    def test_build_bootstrap_steps_includes_portacode_connect_by_default(self):
        steps = _build_bootstrap_steps("svcuser", "pass", "", include_portacode_connect=True)
        self.assertTrue(any(step.get("name") == "portacode_connect" for step in steps))

    def test_build_bootstrap_steps_exposes_portacode_globally_from_venv(self):
        steps = _build_bootstrap_steps("svcuser", "pass", "", include_portacode_connect=False)
        symlink_step = next(step for step in steps if step.get("name") == "ensure_global_portacode_cli")
        self.assertIn("/usr/local/bin/portacode", symlink_step["cmd"])
        self.assertIn("/opt/portacode-venv/bin/portacode", symlink_step["cmd"])

    def test_build_bootstrap_steps_skips_portacode_connect_when_requested(self):
        steps = _build_bootstrap_steps("svcuser", "pass", "", include_portacode_connect=False)
        self.assertFalse(any(step.get("name") == "portacode_connect" for step in steps))

    def test_sanitize_project_paths_keeps_child_relative_markers_raw(self):
        paths = _sanitize_project_paths(["~/.openclaw", "$HOME/app"])

        self.assertEqual(paths, ["~/.openclaw", "$HOME/app"])

    @patch("portacode.connection.handlers.proxmox_infra._run_pct")
    @patch("portacode.connection.handlers.proxmox_infra._resolve_user_data_home", return_value="/home/user/.local/share")
    def test_enforce_service_venv_execstart_shell_quotes_sed_script_with_project_paths(
        self,
        _mock_data_home,
        mock_run_pct,
    ):
        mock_run_pct.return_value = {"returncode": 0, "stdout": "", "stderr": ""}

        _enforce_service_venv_execstart(
            101,
            "root",
            runtime_user="user",
            project_paths=["$HOME/.openclaw", "$HOME/.openclaw/workspace"],
        )

        issued_command = mock_run_pct.call_args.args[1]
        self.assertIn("sed -i ", issued_command)
        self.assertIn("s#^ExecStart=.*#ExecStart=/opt/portacode-venv/bin/python -m portacode connect --non-interactive", issued_command)
        self.assertIn("'\"'\"'$HOME/.openclaw'\"'\"'", issued_command)
        self.assertIn("'\"'\"'$HOME/.openclaw/workspace'\"'\"'", issued_command)

    @patch("portacode.connection.handlers.proxmox_infra.get_infra_snapshot", return_value={})
    @patch("portacode.connection.handlers.proxmox_infra._remove_container_record")
    @patch("portacode.connection.handlers.proxmox_infra._ensure_container_managed")
    @patch("portacode.connection.handlers.proxmox_infra._read_container_record")
    @patch("portacode.connection.handlers.proxmox_infra._get_node_from_config", return_value="pve2")
    @patch("portacode.connection.handlers.proxmox_infra._connect_proxmox", return_value=object())
    @patch("portacode.connection.handlers.proxmox_infra._ensure_infra_configured", return_value={"token_value": "x"})
    @patch("portacode.connection.handlers.cloudflare_forwarding.set_container_forwarding_rules")
    def test_remove_container_succeeds_when_proxmox_container_already_missing(
        self,
        mock_clear_forwarding,
        _mock_configured,
        _mock_connect,
        _mock_get_node,
        mock_read_record,
        mock_ensure_managed,
        mock_remove_record,
        _mock_snapshot,
    ):
        mock_read_record.return_value = {"vmid": 134, "device_id": "42"}
        mock_ensure_managed.side_effect = RuntimeError(
            "500 Internal Server Error: Configuration file 'nodes/pve2/lxc/134.conf' does not exist"
        )

        handler = RemoveProxmoxContainerHandler(control_channel=MagicMock(), context={})
        response = handler.execute({"child_device_id": "42", "ctid": "134"})

        self.assertTrue(response["success"])
        self.assertEqual(response["status"], "deleted")
        self.assertEqual(response["ctid"], "134")
        self.assertIn("already deleted", response["message"])
        mock_clear_forwarding.assert_called_once_with("42", [])
        mock_remove_record.assert_called_once_with(134)
