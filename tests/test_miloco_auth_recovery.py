"""覆盖后台退出、续期停滞、换票保护和重新登录提醒。"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from integrations.miloco_recovery import friday_auth_recovery as recovery
from integrations.miloco_recovery import install


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_supervisor(self):
        for situation, expected in (
            ("healthy", 1),
            ("stalled", 2),
            ("exited", 2),
            ("locked", 1),
            ("inflight", 1),
        ):
            with self.subTest(situation=situation):
                lock = asyncio.Lock()
                if situation == "locked":
                    await lock.acquire()
                owner = SimpleNamespace(
                    _auth_health=SimpleNamespace(is_degraded=False, last_attempt_ts=10),
                    _oauth_info=SimpleNamespace(expires_ts=3000 if situation == "healthy" else 10),
                    _kv_repo={"MIOT_REFRESH_INFLIGHT_KEY": situation == "inflight"},
                    _token_refresh_lock=lock,
                )
                checks = 0
                started = 0
                done = asyncio.Event()
                children = []

                async def native_refresh():
                    nonlocal started
                    started += 1
                    children.append(asyncio.current_task())
                    if situation == "exited" and started == 1:
                        raise RuntimeError("模拟任务异常")
                    await asyncio.Event().wait()

                owner._start_token_refresh_task = native_refresh

                def monotonic():
                    nonlocal checks
                    checks += 1
                    if checks == 6:
                        done.set()
                    return checks * 60

                with (
                    patch.object(recovery, "INTERVAL", 0.003),
                    patch.object(recovery, "time", SimpleNamespace(monotonic=monotonic, time=lambda: 2000)),
                ):
                    task = asyncio.create_task(recovery.supervise(owner))
                    try:
                        await asyncio.wait_for(done.wait(), 2)
                    finally:
                        await recovery.stop_task(task)
                self.assertEqual(started, expected)
                self.assertTrue(all(t.done() for t in children))

    async def test_only_confirmed_login_and_dedup(self):
        db = {}
        repo = Mock(get=db.get, set=lambda k, v: db.update({k: v}), delete=db.pop)
        owner = SimpleNamespace(_kv_repo=repo)
        h = SimpleNamespace(
            is_degraded=True,
            error_code=-10021,
            error_message="96009 invalid refresh token",
            since_ts=100,
            last_success_ts=10,
        )
        with (
            patch.dict("os.environ", {"FRIDAY_RECOVERY_HA_URL": ""}),
            patch.object(
                recovery, "desktop_notice", new=AsyncMock(side_effect=[False, True, True])
            ) as notify,
        ):
            last = await recovery.check_notice(owner, h, 1000, 0)
            self.assertNotIn(recovery.NOTICE_KEY, db)
            await recovery.check_notice(owner, h, 1301, last)
            await recovery.check_notice(owner, h, 1700, 0)
            self.assertEqual(notify.await_count, 2)
            h.is_degraded = False
            await recovery.check_notice(owner, h, 1800, 0)
            self.assertFalse(db)
            h.is_degraded = True
            h.error_code = None
            await recovery.check_notice(owner, h, 2000, 0)
            self.assertEqual(notify.await_count, 2)
            h.error_code = -10021
            h.since_ts = 2000
            await recovery.check_notice(owner, h, 2100, 0)
            self.assertEqual(notify.await_count, 3)

    def test_install_and_remove(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            original = (
                "class Example:\n    async def _start_token_refresh_task(self):\n"
                "        # _token_refresh_lock _auth_health _oauth_info _kv_repo MIOT_REFRESH_INFLIGHT_KEY\n"
                + install.OLD
                + "\n"
            ).encode()
            source = directory / "client.py"
            source.write_bytes(original)
            for _ in range(2):
                install.install(directory)
            self.assertEqual(source.read_text().count(install.NEW), 1)
            install.install(directory, uninstall=True)
            self.assertEqual(source.read_bytes(), original)
            self.assertFalse((directory / "friday_auth_recovery.py").exists())
