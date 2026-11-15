"""KTV 房主的逐曲背景管理；只暴露已制备曲目和本地验证过的背景。"""

from __future__ import annotations

import base64
import io
import json
import re
import stat
from typing import Any

from PIL import Image

from homeplane.intelligence.backgrounds import song_identity


BACKGROUND_ID = re.compile(r"[a-f0-9]{64}")
SONG_FIELDS = ("song_id", "title", "artist", "audio_origin")


class BackgroundManagement:
    def __init__(self, queue, backgrounds):
        self.queue = queue
        self.backgrounds = backgrounds

    def _recorded_song(self, identifier: str) -> dict[str, Any] | None:
        root = self.backgrounds.directory
        path = root / (identifier + ".json")
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 2048:
                return None
            song = json.loads(path.read_text()).get("song_ref")
            if isinstance(song, dict) and set(song) == set(SONG_FIELDS):
                return song
        except (OSError, ValueError, AttributeError):
            pass
        return None

    def candidates(self):
        backgrounds = self.backgrounds
        config = backgrounds.runtime.settings.read()
        result = {}
        with self.queue.lock:
            ready = [dict(row) for row in self.queue.jobs.values()
                     if row.get("state") == "ready" and not row.get("simulated")]
        for song in sorted(ready, key=lambda row: row.get("updated", 0), reverse=True):
            try:
                identifier = song_identity(song, config.image_model)
                if song.get("audio_origin") != "cloud" or backgrounds.public_metadata_for(identifier):
                    result.setdefault(identifier, song)
            except (ValueError, OSError):
                continue
        for path in sorted(backgrounds.directory.glob("*.json"))[:2048]:
            identifier = path.stem
            if not BACKGROUND_ID.fullmatch(identifier) or identifier in result:
                continue
            try:
                song = self._recorded_song(identifier)
                if (song and song_identity(song, config.image_model) == identifier
                        and backgrounds.cached(identifier)
                        and (song.get("audio_origin") != "cloud" or backgrounds.public_metadata_for(identifier))):
                    result[identifier] = song
            except (ValueError, OSError):
                continue
        return result

    def status(self, identifier, song):
        backgrounds = self.backgrounds
        ready = bool(backgrounds.cached(identifier))
        status = ("preparing" if identifier in backgrounds.tasks else "failed" if identifier in backgrounds.failures
                  else "ready" if ready else "missing")
        return {"id": identifier, "title": song["title"], "source": song.get("audio_origin") or "song",
                "status": status, "ready": ready}

    def preview(self, identifier):
        data = self.backgrounds.read_image(identifier)
        if not data:
            return None
        with Image.open(io.BytesIO(data)) as frame:
            frame.thumbnail((320, 180), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            frame.convert("RGB").save(output, format="JPEG", quality=82, optimize=True)
        if output.tell() > 65536:
            return None
        return "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode("ascii")

    def daily_remaining(self):
        runtime = self.backgrounds.runtime
        usage = runtime.ledger.status(runtime.settings.read())
        return max(0, usage["limits"]["images"] - usage["used"]["images"])
