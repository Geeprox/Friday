"""KTV 房主的逐曲背景管理；只暴露已制备曲目和本地验证过的背景。"""

from __future__ import annotations

import base64
import io
import re

from PIL import Image

from homeplane.intelligence.budget import EnhancementUnavailable


BACKGROUND_ID = re.compile(r"[a-f0-9]{64}")


class BackgroundManagement:
    def __init__(self, queue, backgrounds):
        self.queue = queue
        self.backgrounds = backgrounds

    def candidates(self):
        backgrounds = self.backgrounds
        config = backgrounds.runtime.settings.read()
        result = {}
        with self.queue.lock:
            ready = [dict(row) for row in self.queue.jobs.values()
                     if row.get("state") == "ready" and not row.get("simulated")]
        for song in sorted(ready, key=lambda row: row.get("updated", 0), reverse=True):
            try:
                identifier = backgrounds.identifier_for(song, model=config.image_model)
                result.setdefault(identifier, song)
            except (ValueError, OSError):
                continue
        for path in sorted(backgrounds.directory.glob("*.json"))[:2048]:
            identifier = path.stem
            if not BACKGROUND_ID.fullmatch(identifier) or identifier in result:
                continue
            try:
                song = backgrounds.song_ref_for(identifier)
                public = backgrounds.public_metadata_for(identifier) if song else None
                if song and backgrounds.identifier_for(song, model=config.image_model, public=public) == identifier:
                    result[identifier] = song
            except (ValueError, OSError):
                continue
        return result

    def status(self, identifier, song):
        backgrounds = self.backgrounds
        ready = bool(backgrounds.cached(identifier))
        failure = backgrounds.failures.get(identifier)
        status = ("preparing" if identifier in backgrounds.tasks else "failed" if failure
                  else "ready" if ready else "missing")
        item = {"id": identifier, "title": song["title"], "source": song.get("audio_origin") or "song",
                "status": status, "ready": ready}
        if status == "failed":
            item["reason"] = failure[0]
        return item

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

    def policy(self):
        runtime = self.backgrounds.runtime
        config = runtime.settings.read()
        usage = runtime.ledger.status(config)
        remaining = None if self.backgrounds.debug else max(0, config.daily_images - usage["used"]["images"])
        policy = {"enabled": config.cloud_images, "daily_limit": config.daily_images, "daily_remaining": remaining}
        if config.cloud_images:
            try:
                runtime.client.authorize("images")
            except EnhancementUnavailable as exc:
                policy["generation_issue"] = exc.code
            else:
                for block in usage["blocks"]:
                    if block["capability"] in {"images", "all"}:
                        policy["generation_issue"] = block["code"]
                        break
        return policy

    def listing(self):
        items = [self.status(key, song) for key, song in list(self.candidates().items())[:128]]
        return {"items": items, **self.policy()}
