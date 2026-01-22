from unittest import TestCase

from portacode.connection.handlers.proxmox_infra import _build_bootstrap_steps


class ProxmoxInfraHandlerTests(TestCase):
    def test_build_bootstrap_steps_includes_portacode_connect_by_default(self):
        steps = _build_bootstrap_steps("svcuser", "pass", "", include_portacode_connect=True)
        self.assertTrue(any(step.get("name") == "portacode_connect" for step in steps))

    def test_build_bootstrap_steps_skips_portacode_connect_when_requested(self):
        steps = _build_bootstrap_steps("svcuser", "pass", "", include_portacode_connect=False)
        self.assertFalse(any(step.get("name") == "portacode_connect" for step in steps))
