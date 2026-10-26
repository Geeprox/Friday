"""Task-private HA transport and the explicitly selected living-room speaker."""

import asyncio
import json
import threading
import time
import urllib.parse
import urllib.request

from homeplane.embodied.paths import PRIVATE

VOICE_PAUSED = PRIVATE / "voice-paused"


class HA:
    def __init__(self):
        self.lock = threading.Lock()

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


class Speaker:
    def __init__(self):
        self.ha = HA()
        self.state = {
            "device": "已选择的播报设备",
            "transport": "HA notify play_text",
            "status": "ready_to_test",
            "physical_audio_verified": False,
        }

    async def say(self, text):
        if not isinstance(text, str) or not 1 <= len(text) <= 1200:
            raise ValueError("invalid_reply")
        if VOICE_PAUSED.exists():
            self.state.update(status="paused")
            return dict(self.state)
        from homeplane.embodied.paths import configuration

        if configuration().get("speech_output") == "homepod":
            from homeplane.embodied.airplay import play
            self.state = await play(self.ha, text)
            return dict(self.state)
        if configuration().get("speech_output") in {"media_player", "jbl"}:
            from homeplane.embodied.synthesis import play_media_player

            self.state = await play_media_player(self.ha, text)
            return dict(self.state)
        self.state.update(status="requesting")
        try:
            await asyncio.to_thread(
                self.ha.request,
                "services/notify/send_message",
                {
                    "entity_id": configuration()["speaker_notify"],
                    "message": json.dumps([text], ensure_ascii=False),
                },
            )
            self.state.update(status="accepted", requested_at=time.time())
            return dict(self.state)
        except Exception as e:
            self.state.update(status="unavailable", error_class=type(e).__name__)
            return dict(self.state)
