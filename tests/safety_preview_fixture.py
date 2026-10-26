"""Isolated synthetic safety UI preview. Never opens cameras or speaks.

Run: .venv/bin/python tests/safety_preview_fixture.py
"""

import io
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn
from fastapi import APIRouter, FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageDraw

from homeplane.embodied import safety_api

folder = tempfile.TemporaryDirectory(prefix="friday-safety-preview-")
safety_api.PRIVATE = Path(folder.name)
app = FastAPI()
router = APIRouter()
runtime = safety_api.install(router, SimpleNamespace(process=None))


class SilentSpeaker:
    async def say(self, text):
        return dict(status="paused")


runtime.speaker = SilentSpeaker()
frames = []
for index in range(6):
    image = Image.new("RGB", (640, 360), "#edeeea")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 260, 640, 360), fill="#d9dcd5")
    draw.rounded_rectangle((70, 130, 270, 245), 12, fill="#c6cbc0")
    draw.line([(330, 275), (410, 280), (470, 295)], fill="#5c7163", width=12)
    draw.line([(385, 277), (425, 250)], fill="#5c7163", width=9)
    draw.ellipse((300, 255, 335, 290), fill="#5c7163")
    draw.text((20, 20), "SYNTHETIC FIXTURE / NO REAL CAMERA", fill="#444a42")
    output = io.BytesIO()
    image.save(output, "JPEG")
    frames.append((1800000000 + index * .5, output.getvalue()))
runtime.store.create(dict(kind="fall", severity="test", simulated=True, status="test",
    message="测试播报：检测到 Example Person 疑似跌倒，请立即查看。", person_name="Example Person", person_id=None,
    camera="synthetic-camera", session="synthetic-session", track="7", basis="synthetic_fixture"), frames)
app.include_router(router)
app.mount("/static", StaticFiles(directory=Path(__file__).resolve().parents[1] / "homeplane/static"))


@app.get("/")
async def index():
    return HTMLResponse("""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1"><title>Friday 智能预警预览</title>
    <link rel="stylesheet" href="/static/style.css"><main style="padding-top:40px">
    <header class="page-header"><h1>智能预警</h1><p class="muted">合成场景预览 · 无摄像头接入与真实声音</p></header>
    <div id="preview"></div></main><script type="module">
    import {mountSafety} from '/static/safety.js';mountSafety(document.getElementById('preview'));
    </script></html>""")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8879)
