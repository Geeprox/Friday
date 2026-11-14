"""Task-private HA transport and the explicitly selected living-room speaker."""

import asyncio
import json
import re
import threading
import time
import urllib.parse
import urllib.request

from homeplane.embodied.paths import PRIVATE, configuration
from homeplane.embodied.speech_http import AsyncHA
from homeplane.embodied.speech_lifecycle import SpeechExpired, output_lock, run_output

VOICE_PAUSED = PRIVATE / "voice-paused"


class HA:
    def __init__(self):
        self.lock = threading.Lock()
        self.asynchronous = AsyncHA(lambda: PRIVATE)

    def request(self, path, data=None):
        with self.lock:
            file = PRIVATE / "ha-session.json"
            c = json.loads(file.read_text())
            base = c["ha_url"].rstrip("/")
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            if c["expires_at"] < time.time() + 60:
                fresh = json.load(
                    opener.open(
                        urllib.request.Request(
                            base + "/auth/token",
                            data=urllib.parse.urlencode(
                                {
                                    "grant_type": "refresh_token",
                                    "refresh_token": c["refresh_token"],
                                    "client_id": c["client_id"],
                                }
                            ).encode(),
                        ),
                        timeout=10,
                    )
                )
                c.update(fresh)
                c["expires_at"] = time.time() + c.get("expires_in", 1800)
                file.write_text(json.dumps(c))
                file.chmod(0o600)
            req = urllib.request.Request(
                base + "/api/" + path,
                headers={"Authorization": "Bearer " + c["access_token"], "Content-Type": "application/json"},
                data=None if data is None else json.dumps(data, ensure_ascii=False).encode(),
            )
            with opener.open(req, timeout=20) as response:
                return json.load(response)

    async def arequest(self, path, data=None, *, guard):
        return await self.asynchronous.request(path, data, guard=guard)


class Speaker:
    def __init__(self):
        self.ha = HA()
        self.state = dict(status="idle", physical_audio_verified=False)

    async def say(self, text, *, current=None, revalidate=None):
        if not isinstance(text, str) or not 1 <= len(text) <= 1200:
            raise ValueError("invalid_reply")
        if VOICE_PAUSED.exists():
            self.state = dict(status="paused", physical_audio_verified=False)
            return dict(self.state)
        try:
            config = configuration()
            keys = ("speech_output", "speaker_entity", "speaker_notify", "viewer_host", "viewer_port")
            selection = {k: config.get(k) for k in keys}
            homepod = None
            if config.get("speech_output") == "homepod":
                from .airplay import _target

                homepod = _target()["identifier"]

            def valid():
                if VOICE_PAUSED.exists() or current is not None and current() is not True:
                    return False
                now = configuration()
                if {k: now.get(k) for k in keys} != selection:
                    return False
                if homepod is not None:
                    from .airplay import _target

                    if _target()["identifier"] != homepod:
                        return False
                return True

            async def perform(guard):
                try:
                    await guard.checkpoint()
                    if config.get("speech_output") == "homepod":
                        from .airplay import play

                        return await play(self.ha, text, guard=guard)
                    if config.get("speech_output") in {"media_player", "jbl"}:
                        from .synthesis import play_media_player

                        return await play_media_player(self.ha, text, config=config, guard=guard)
                    entity = config.get("speaker_notify")
                    if not isinstance(entity, str) or not re.fullmatch(r"notify\.[a-z0-9_]+", entity):
                        raise ValueError("speech_output_unavailable")
                    await self.ha.arequest(
                        "services/notify/send_message",
                        {
                            "entity_id": entity,
                            "message": json.dumps([text], ensure_ascii=False),
                        },
                        guard=guard,
                    )
                    return dict(status="accepted", requested_at=time.time(), physical_audio_verified=False)
                except SpeechExpired:
                    return guard.receipt()
                except Exception as error:
                    return dict(
                        status="unconfirmed" if guard.dispatched else "unavailable",
                        error_class=type(error).__name__,
                        physical_audio_verified=False,
                    )

            async def exclusive(guard):
                with output_lock(PRIVATE) as acquired:
                    if not acquired:
                        return dict(status="busy", physical_audio_verified=False)
                    receipt = await perform(guard)
                    if receipt["status"] == "unavailable":
                        # 只有持有输出权限的任务能确认故障是否发生在发送前。
                        return dict(receipt, dispatch_attempted=guard.dispatched)
                    return receipt

            self.state = {**await run_output(exclusive, valid, revalidate=revalidate), "physical_audio_verified": False}
            if self.state["status"] == "expired" and VOICE_PAUSED.exists():
                self.state["status"] = "paused"
        except asyncio.CancelledError:
            self.state = dict(status="unconfirmed", physical_audio_verified=False)
            raise
        except SpeechExpired:
            self.state = dict(status="expired", physical_audio_verified=False)
        except Exception as error:
            self.state = dict(
                status="unavailable", error_class=type(error).__name__, physical_audio_verified=False
            )
        return dict(self.state)
