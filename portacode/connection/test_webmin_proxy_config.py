from unittest import TestCase

from portacode.connection.webmin_proxy_config import (
    _replace_or_append_setting,
    resolve_webmin_public_host,
)


class WebminProxyConfigTests(TestCase):
    def test_replace_or_append_setting_replaces_existing_key_once(self):
        original = "foo=1\nreferers=\nbar=2\n"
        updated = _replace_or_append_setting(original, "referers", "1_799.exposify.net")
        self.assertEqual(updated, "foo=1\nreferers=1_799.exposify.net\nbar=2\n")

    def test_replace_or_append_setting_appends_missing_key(self):
        updated = _replace_or_append_setting("foo=1\n", "redirect_host", "1_799.exposify.net")
        self.assertEqual(updated, "foo=1\nredirect_host=1_799.exposify.net\n")

    def test_resolve_webmin_public_host_picks_port_12321(self):
        host = resolve_webmin_public_host(
            [
                {"port": 443, "hostname": "799.exposify.net"},
                {"port": 12321, "hostname": "1_799.exposify.net"},
                {"port": 12322, "hostname": "2_799.exposify.net"},
            ]
        )
        self.assertEqual(host, "1_799.exposify.net")
