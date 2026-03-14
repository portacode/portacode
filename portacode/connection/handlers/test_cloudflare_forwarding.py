import json
from unittest import TestCase
from unittest.mock import patch

from portacode.connection.handlers.cloudflare_forwarding import (
    EXPOSED_SERVICES_JSON_PATH,
    _build_exposed_services_env_map,
    _sync_exposed_services_into_container,
)


class CloudflareForwardingEnvTests(TestCase):
    def test_build_exposed_services_env_map_adds_indexed_public_host_aliases(self):
        env_map = _build_exposed_services_env_map(
            [
                {"hostname": "123.example.com", "url": "https://123.example.com", "port": 443},
                {"hostname": "1_123.example.com", "url": "https://1_123.example.com", "port": 12321},
                {"hostname": "2_123.example.com", "url": "https://2_123.example.com", "port": 12322},
            ]
        )

        self.assertEqual(env_map["PORTACODE_PUBLIC_HOST"], "123.example.com")
        self.assertEqual(env_map["PORTACODE_PUBLIC_HOST_1"], "123.example.com")
        self.assertEqual(env_map["PORTACODE_PUBLIC_HOST_2"], "1_123.example.com")
        self.assertEqual(env_map["PORTACODE_PUBLIC_HOST_3"], "2_123.example.com")

    @patch("portacode.connection.handlers.cloudflare_forwarding._push_root_file_to_container")
    @patch("portacode.connection.handlers.cloudflare_forwarding._resolve_device_vmid", return_value=321)
    def test_sync_exposed_services_into_container_pushes_only_canonical_json(
        self,
        _mock_resolve_vmid,
        mock_push,
    ):
        exposed_ports = [
            {"hostname": "123.example.com", "url": "https://123.example.com", "port": 443},
        ]

        _sync_exposed_services_into_container(
            container_device_id="123",
            exposed_ports=exposed_ports,
            proxmox=object(),
            node="pve",
        )

        mock_push.assert_called_once()
        vmid, path, data = mock_push.call_args.args[:3]
        self.assertEqual(vmid, 321)
        self.assertEqual(path, EXPOSED_SERVICES_JSON_PATH)
        self.assertEqual(
            json.loads(data.decode("utf-8")),
            {
                "device_id": "123",
                "exposed_services": exposed_ports,
            },
        )
        self.assertEqual(mock_push.call_args.kwargs["mode"], 0o644)
