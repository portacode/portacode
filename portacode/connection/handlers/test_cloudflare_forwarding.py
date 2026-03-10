from unittest import TestCase

from portacode.connection.handlers.cloudflare_forwarding import _build_exposed_services_env_map


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
