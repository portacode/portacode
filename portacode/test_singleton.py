from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest import TestCase

from portacode.singleton import GatewayLock


class GatewayLockTests(TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.lock_path = Path(self.tempdir.name) / "gateway.lock"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_only_one_lock_can_be_held(self):
        first = GatewayLock(self.lock_path)
        second = GatewayLock(self.lock_path)
        self.assertTrue(first.acquire())
        first.write_pid(os.getpid())
        self.assertFalse(second.acquire())
        self.assertEqual(second.read_pid(), os.getpid())
        first.release()
        self.assertTrue(second.acquire())
        second.release()

    def test_release_is_idempotent(self):
        lock = GatewayLock(self.lock_path)
        self.assertTrue(lock.acquire())
        lock.release()
        lock.release()
