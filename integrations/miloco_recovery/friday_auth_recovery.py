"""随 Miloco 后台运行的授权巡检，令牌交换始终由 Miloco 自身串行执行。"""

import asyncio
import json
import logging
import os
import shutil
import sys
import time
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

LOGGER = logging.getLogger(__name__)
NOTICE_KEY = "FRIDAY_AUTH_NOTICE_SINCE"
TITLE = "Miloco 摄像头需要重新登录"
MESSAGE = "小米已拒绝原授权。请打开 Miloco，用原米家账号重新绑定。"
INTERVAL = 60
STALL_GRACE = 20 * 60
RESTART_COOLDOWN = 2 * 3600


def login_incident(health):
    if not health.is_degraded or not health.since_ts:
        return None
    detail = (health.error_message or "").lower()
    if health.error_code in {-10020, -10021} and any(
        word in detail
        for word in (
            "96009",
            "invalid refresh token",
            "invalid_grant",
            "refresh token expired",
            "refresh token revoked",
            "unauthorized(401)",
        )
    ):
        return str(health.since_ts)
    return None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def ha_notice(dismiss=False):
    """可选的无桌面提醒，通过已配置的 HA 留下一条持久通知。"""
    url = os.environ["FRIDAY_RECOVERY_HA_URL"].rstrip("/")
    parts = urlsplit(url)
    if (
        not parts.hostname
        or parts.username
        or parts.password
        or parts.path
        or parts.query
        or parts.fragment
        or not (
            parts.scheme == "https"
            or (parts.scheme == "http" and parts.hostname in {"localhost", "127.0.0.1", "::1"})
        )
    ):
        raise ValueError("invalid_ha_url")
    token = Path(os.environ["FRIDAY_RECOVERY_HA_TOKEN_FILE"]).read_text().strip()
    payload = {"notification_id": "friday_miloco_camera_login"}
    service = "dismiss" if dismiss else "create"
    if not dismiss:
        payload.update(title=TITLE, message=MESSAGE)
    req = Request(
        f"{url}/api/services/persistent_notification/{service}",
        data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    with build_opener(ProxyHandler({}), NoRedirect()).open(req, timeout=6) as response:
        return response.status == 200


async def desktop_notice():
    if sys.platform == "darwin":
        args = ["/usr/bin/osascript", "-e", f'display notification "{MESSAGE}" with title "{TITLE}"']
    else:
        env_bus = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
        session_bus = Path(f"/run/user/{os.getuid()}/bus")
        notify = shutil.which("notify-send")
        if not notify or not (env_bus or session_bus.is_socket()):
            return False
        args = [notify, "--urgency=critical", TITLE, MESSAGE]
    env = os.environ.copy()
    if sys.platform.startswith("linux") and not env.get("DBUS_SESSION_BUS_ADDRESS"):
        env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=" + str(session_bus)
    proc = await asyncio.create_subprocess_exec(
        *args, env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )
    try:
        return await asyncio.wait_for(proc.wait(), 8) == 0
    finally:
        if proc.returncode is None:
            with suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()


async def stop_task(task):
    task.cancel()
    # 等待原任务完全退出，防止新旧任务同时兑换一次性凭据。
    await asyncio.gather(task, return_exceptions=True)


async def check_notice(owner, health, tick, last_notice_attempt):
    incident = login_incident(health)
    use_ha = bool(os.environ.get("FRIDAY_RECOVERY_HA_URL"))
    sent = owner._kv_repo.get(NOTICE_KEY)
    if sent and not health.is_degraded and health.last_success_ts and tick - last_notice_attempt >= 300:
        if use_ha:
            await asyncio.to_thread(ha_notice, True)
        owner._kv_repo.delete(NOTICE_KEY)
    if incident and tick - last_notice_attempt >= 300 and sent != incident:
        last_notice_attempt = tick
        delivered = await asyncio.to_thread(ha_notice) if use_ha else await desktop_notice()
        if delivered:
            owner._kv_repo.set(NOTICE_KEY, incident)
    return last_notice_attempt


async def supervise(owner):
    LOGGER.info("Friday 摄像头授权巡检已随 Miloco 启动")
    task = asyncio.create_task(owner._start_token_refresh_task())
    first_stalled = None
    last_restart = last_notice_attempt = float("-inf")
    try:
        while True:
            await asyncio.sleep(INTERVAL)
            try:
                tick = time.monotonic()
                health = owner._auth_health
                try:
                    last_notice_attempt = await check_notice(owner, health, tick, last_notice_attempt)
                except (OSError, ValueError, KeyError, TimeoutError) as error:
                    last_notice_attempt = tick
                    LOGGER.warning("本机登录提醒暂缓 (%s)", type(error).__name__)
                if task.done():
                    last_restart = tick
                    await asyncio.gather(task, return_exceptions=True)
                    task = asyncio.create_task(owner._start_token_refresh_task())
                    first_stalled = None
                    continue
                auth = owner._oauth_info
                if (
                    not auth
                    or health.is_degraded
                    or owner._token_refresh_lock.locked()
                    or time.time() - max(auth.expires_ts, health.last_attempt_ts or 0) < STALL_GRACE
                    or owner._kv_repo.get("MIOT_REFRESH_INFLIGHT_KEY")
                ):
                    first_stalled = None
                    continue
                if first_stalled is None:
                    first_stalled = tick
                elif tick - first_stalled >= INTERVAL and tick - last_restart >= RESTART_COOLDOWN:
                    await stop_task(task)
                    task = asyncio.create_task(owner._start_token_refresh_task())
                    first_stalled = None
                    last_restart = tick
                    LOGGER.warning("已重新启动停滞的 Miloco 续期任务")
            except Exception as error:
                LOGGER.warning("摄像头授权巡检暂缓 (%s)", type(error).__name__)
    finally:
        await stop_task(task)
