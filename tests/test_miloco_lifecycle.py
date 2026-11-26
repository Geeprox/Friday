"""验证首次安装和上游替换环境后的维护生命周期。"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from integrations.miloco_recovery import install, lifecycle


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.directory = self.base / "miot"
        self.directory.mkdir()
        self.cli = self.base / "miloco-cli"
        self.cli.touch()
        self.client = self.directory / "client.py"
        self.original = (
            "class MiotProxy:\n    async def _start_token_refresh_task(self):\n"
            "        # _token_refresh_lock _auth_health _oauth_info _kv_repo MIOT_REFRESH_INFLIGHT_KEY\n"
            + install.OLD
            + "\n"
        ).encode()
        self.client.write_bytes(self.original)
        self.pid = 12
        self.restarts = 0
        self.fail_next = False
        for key, val in (
            ("BASE", self.base),
            ("installing", lambda: False),
            ("cli_json", lambda *args: {"value": "/local/runtime/python"}),
            ("running_pid", lambda cli: self.pid),
            ("run", self.run_command),
        ):
            self.enter_patch(patch.object(lifecycle, key, val))
        self.enter_patch(patch.object(subprocess, "run", side_effect=self.probe))

    def enter_patch(self, mock):
        self.addCleanup(mock.stop)
        mock.start()

    def probe(self, command, **kwargs):
        self.assertEqual(command[-1], "--check")
        changed = install.install(self.directory, check_only=True)
        return subprocess.CompletedProcess(command, 10 if changed else 0)

    def run_command(self, command, **kwargs):
        if command[0] == self.cli:
            self.assertEqual(command[1:], ["service", "restart"])
            if self.fail_next:
                self.fail_next = False
                raise OSError("模拟重启失败")
            self.restarts += 1
            self.pid += 1
        else:
            install.install(self.directory)

    def test_first_install_upgrade_and_repeat(self):
        lifecycle.reconcile(self.cli)
        self.assertEqual(self.restarts, 1)
        self.client.write_bytes(self.original)
        (self.directory / "friday_auth_recovery.py").unlink()
        with patch.object(lifecycle, "installing", return_value=True):
            self.assertEqual(lifecycle.reconcile(self.cli), "upgrading")
            self.assertEqual(self.client.read_bytes(), self.original)
        lifecycle.reconcile(self.cli)
        self.assertIn(install.NEW, self.client.read_text())
        before = self.client.stat().st_mtime_ns
        lifecycle.reconcile(self.cli)
        self.assertEqual(self.restarts, 2)
        self.assertEqual(self.client.stat().st_mtime_ns, before)

    def test_retry_restart_then_respect_stop(self):
        self.fail_next = True
        with self.assertRaises(OSError):
            lifecycle.reconcile(self.cli)
        self.assertEqual(json.loads((self.base / "pending.json").read_text())["pid"], 12)
        lifecycle.reconcile(self.cli)
        self.assertEqual(self.restarts, 1)
        self.pid = None
        self.client.write_bytes(self.original)
        lifecycle.reconcile(self.cli)
        self.assertEqual(self.restarts, 1)

    def test_unknown_version_preserved(self):
        self.client.write_bytes(b"class Different: pass\n")
        with self.assertRaises(SystemExit):
            lifecycle.reconcile(self.cli)
        self.assertEqual(self.client.read_bytes(), b"class Different: pass\n")
        self.assertEqual(self.restarts, 0)
