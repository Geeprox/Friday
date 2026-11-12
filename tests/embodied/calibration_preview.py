"""Loopback-only synthetic editor review. Never imports the perception owner."""

import argparse
import ast
import asyncio
import base64
import datetime
import fcntl
import hashlib
import json
import os
import re
import socket
import struct
import time
from contextlib import asynccontextmanager
from pathlib import Path

import cv2
import numpy as np
import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse

from homeplane.embodied.calibration_frames import CalibrationFrames
from homeplane.embodied.calibration_save import CalibrationSaver
from homeplane.embodied.calibration_store import CalibrationStore
from homeplane.embodied.room_engine import DEFAULT, calibration_draft
from homeplane.embodied.static_page import static_asset, static_page

PROJECT = Path(__file__).resolve().parents[2]
STATIC = PROJECT / "homeplane/embodied/static"


def actual(name, scope):
    node = next(
        n
        for n in ast.parse((PROJECT / "homeplane/embodied/worker.py").read_text()).body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == name
    )
    node.decorator_list = []
    exec(compile(ast.Module(body=[node], type_ignores=[]), "<original editor endpoint>", "exec"), scope)
    return scope[name]


def application(root):
    frames = CalibrationFrames()
    source = dict(worker="SIMULATED-editor", generation=1, motion_generation=1)
    mode = {"value": "auto"}
    events = []
    reference_hashes = []
    current_config = {"value": calibration_draft(DEFAULT)}

    def record(kind):
        events.append(kind)
        (root / "events.json").write_text(
            json.dumps(dict(events=events[-30:], reference_hashes=reference_hashes[-8:]))
        )

    def invalidate():
        frames.clear()
        source["motion_generation"] += 1
        record("invalidate")

    saver = CalibrationSaver(root / "private", invalidate)
    scope = dict(
        HTTPException=HTTPException,
        Request=Request,
        json=json,
        base64=base64,
        calibration_source=lambda: dict(source),
        calibration_frames=frames,
        calibration_saver=saver,
        state={"status": "live"},
    )
    get, post = actual("calibration", scope), actual("save_calibration", scope)

    @asynccontextmanager
    async def lifespan(_):
        try:
            yield
        finally:
            await saver.close()

    app = FastAPI(lifespan=lifespan)

    @app.get("/")
    async def index():
        html = static_page(STATIC / "index.html")
        html = re.sub(r"<h1>.*?</h1>", "<h1>模拟校准</h1>", html, count=1)
        html = html.replace("家庭互动 · 全屋摄像头共享识别", "合成画面 · 未连接摄像头")
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

    @app.get("/assets/{name}")
    async def asset(name: str, request: Request):
        if name not in {"index.js", "index.css"}:
            raise HTTPException(404)
        return static_asset(STATIC / name, request.headers)

    @app.get("/api/calibration")
    async def calibration():
        record("get")
        if (root / "scenario").read_text().strip() == "unavailable":
            raise HTTPException(409, detail="等待画面稳定后再校准")
        value = await get()
        reference_hashes.append(hashlib.sha256(base64.b64decode(value["_reference"]["jpeg"])).hexdigest())
        record("reference")
        return value

    @app.post("/api/calibration")
    async def save(request: Request):
        result = await post(request)
        config, picture, revision = CalibrationStore(root / "private").read()
        current_config["value"] = calibration_draft(config)
        (root / "saved.json").write_text(
            json.dumps(
                dict(
                    revision=revision,
                    reference_sha256=hashlib.sha256(picture).hexdigest(),
                    source="synthetic image only",
                    metric=config["metric"],
                )
            )
        )
        record("saved")
        return result

    @app.post("/api/camera")
    async def camera(request: Request):
        value = await request.json()
        mode["value"] = value["mode"]
        record("mode_" + mode["value"])
        return dict(mode=mode["value"])

    @app.websocket("/ws")
    async def live(ws: WebSocket):
        await ws.accept()
        pixels = np.full((360, 640, 3), 236, np.uint8)
        rng = np.random.default_rng(14)
        for x in range(24, 620, 32):
            for y in range(24, 340, 32):
                color = int(rng.integers(70, 200))
                cv2.rectangle(pixels, (x, y), (x + 8, y + 8), (color, color, color), -1)
        try:
            while True:
                image = pixels.copy()
                cv2.rectangle(image, (170, 130), (480, 205), (245, 245, 245), -1)
                cv2.putText(
                    image,
                    "SIMULATED " + str(int(time.monotonic()) % 1000),
                    (180, 175),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.85,
                    (55, 55, 55),
                    2,
                )
                jpeg = cv2.imencode(".jpg", image)[1].tobytes()
                layout = dict(
                    mode="unconfigured",
                    metric=False,
                    note="合成画面",
                    registration={"status": "locked"},
                    seats=[],
                    furniture=[],
                    outline=[],
                    pointing=[],
                    observed_furniture=[],
                )
                meta = dict(
                    status="live",
                    width=640,
                    height=360,
                    room=layout,
                    people=[],
                    hands=[],
                    received_at=time.time(),
                    camera_control=dict(mode=mode["value"], reason="模拟控制"),
                )
                frames.publish(current_config["value"], jpeg, meta, source, meta["received_at"])
                encoded = json.dumps(meta).encode()
                await ws.send_bytes(struct.pack(">II", len(encoded), len(jpeg)) + encoded + jpeg)
                await asyncio.sleep(0.2)
        except Exception:
            pass

    return app


async def serve(root):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    (root / "private").mkdir(exist_ok=True, mode=0o700)
    (root / "scenario").write_text("normal")
    manifest = root / "manifest.json"
    state = dict(
        source_project=str(PROJECT),
        purpose="Synthetic calibration editor browser review",
        created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        status="running",
        pid=os.getpid(),
        toolchain="Python3.12 / Web / macOS",
        source_files={
            str(p.relative_to(PROJECT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [STATIC / "index.js", STATIC / "index.css", STATIC / "index.html", Path(__file__)]
        },
        reproduce="python -B -m tests.embodied.calibration_preview --root <isolated review root>",
    )
    with (root / "active.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        state["port"] = sock.getsockname()[1]
        manifest.write_text(json.dumps(state, indent=2) + "\n")
        print(json.dumps(dict(pid=os.getpid(), port=state["port"], manifest=str(manifest))), flush=True)
        try:
            await uvicorn.Server(
                uvicorn.Config(application(root), log_level="warning", access_log=False)
            ).serve(sockets=[sock])
        finally:
            sock.close()
            state.update(status="stopped", ended_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
            manifest.write_text(json.dumps(state, indent=2) + "\n")
            fcntl.flock(lock, fcntl.LOCK_UN)
            (root / "active.lock").unlink(missing_ok=True)
    (root / "active.lock").unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    try:
        asyncio.run(serve(parser.parse_args().root))
    except KeyboardInterrupt:
        pass
