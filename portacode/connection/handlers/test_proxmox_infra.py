from unittest import TestCase
from unittest.mock import MagicMock, patch

from portacode.connection.handlers.proxmox_infra import (
    RemoveProxmoxContainerHandler,
    _build_bootstrap_steps,
)


class ProxmoxInfraHandlerTests(TestCase):
    def test_build_bootstrap_steps_includes_portacode_connect_by_default(self):
        steps = _build_bootstrap_steps("svcuser", "pass", "", include_portacode_connect=True)
        self.assertTrue(any(step.get("name") == "portacode_connect" for step in steps))

    def test_build_bootstrap_steps_skips_portacode_connect_when_requested(self):
        steps = _build_bootstrap_steps("svcuser", "pass", "", include_portacode_connect=False)
        self.assertFalse(any(step.get("name") == "portacode_connect" for step in steps))

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
