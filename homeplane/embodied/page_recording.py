"""Bounded local recordings of the composed game page, explicitly user enabled."""

import json
import secrets
import time
from pathlib import Path


class PageRecording:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(exist_ok=True)
        self.path = None
        self.mime = None
        self.state = {
            "armed": True,
            "status": "armed",
            "bytes": 0,
            "max_seconds": 180,
            "audio": False,
            "composition": "game_and_recognition_overlay",
            "id": None,
            "file": None,
        }
        previous = self.root / "latest.json"
        if previous.exists():
            try:
                saved = json.loads(previous.read_text())
                name = saved.get("file")
                if (
                    name
                    and Path(name).name == name
                    and Path(name).suffix in (".webm", ".mp4")
                    and (self.root / name).is_file()
                ):
                    self.state.update(saved)
                    self.path = self.root / name
                    self.mime = saved.get("mime")
                    if self.state["status"] in ("recording", "stopping"):
                        self.state.update(status="interrupted", armed=False, reason="service_restarted")
            except (ValueError, OSError, KeyError):
                pass

    def persist(self):
        (self.root / "latest.json").write_text(json.dumps(self.state, ensure_ascii=False, indent=2))

    def public(self):
        if (
            self.state["status"] == "recording"
            and time.time() - self.state.get("last_chunk_at", self.state["started_at"]) > 20
        ):
            self.state.update(status="interrupted", armed=False, reason="recording_page_disconnected")
            self.persist()
        return dict(self.state)

    def arm(self, enabled):
        self.state["armed"] = bool(enabled)
        if self.state["status"] in ("recording", "stopping"):
            if not enabled:
                self.state["status"] = "stopping"
        else:
            self.state["status"] = "armed" if enabled else "off"
        self.persist()
        return self.public()

    def start(self, mime):
        if self.public()["status"] in ("recording", "stopping") or not self.state["armed"]:
            raise ValueError("recording_not_armed_or_busy")
        allowed = {
            "video/webm;codecs=vp8": "webm",
            "video/webm": "webm",
            "video/mp4": "mp4",
            "video/mp4;codecs=avc1.42E01E": "mp4",
        }
        if mime not in allowed:
            raise ValueError("recording_format_unsupported")
        token = secrets.token_hex(12)
        name = time.strftime("gameplay-%Y%m%d-%H%M%S") + "-" + token[:4] + "." + allowed[mime]
        self.path = self.root / name
        self.path.touch(exist_ok=False)
        self.mime = mime
        for field in ("finished_at", "duration_seconds"):
            self.state.pop(field, None)
        self.state.update(
            status="recording",
            id=token,
            file=name,
            bytes=0,
            started_at=time.time(),
            last_chunk_at=time.time(),
            mime=mime,
            reason=None,
        )
        self.persist()
        return self.public()

    def append(self, token, body):
        if (
            token != self.state.get("id")
            or self.state["status"] not in ("recording", "stopping")
            or self.path is None
        ):
            raise ValueError("recording_not_owned")
        if len(body) > 8 * 1024 * 1024 or self.state["bytes"] + len(body) > 120 * 1024 * 1024:
            raise ValueError("recording_size_limit")
        if time.time() - self.state["started_at"] > 195:
            raise ValueError("recording_time_limit")
        with self.path.open("ab") as out:
            out.write(body)
        self.state["bytes"] += len(body)
        self.state["last_chunk_at"] = time.time()
        return {"bytes": self.state["bytes"]}

    def finish(self, token):
        if (
            token != self.state.get("id")
            or self.state["status"] not in ("recording", "stopping")
            or not self.state["bytes"]
        ):
            raise ValueError("recording_not_ready")
        self.state.update(
            status="saved",
            armed=False,
            finished_at=time.time(),
            duration_seconds=round(time.time() - self.state["started_at"], 2),
        )
        self.persist()
        return self.public()
