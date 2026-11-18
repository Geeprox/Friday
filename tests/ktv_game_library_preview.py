"""Manual synthetic browser fixture; never starts Friday/KTV or contacts a device.

Run with .venv/bin/python -B tests/ktv_game_library_preview.py --scenario ready.
Stop with Ctrl-C. Cache ownership/cleanup is the same as other KTV build tools.
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import struct
import sys
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.ktv_build_workspace import build_workspace  # noqa: E402

STATIC_ROOT = ROOT / "homeplane/static"
PARTY_AUDIO_FILE = ROOT / "integrations/ktv_public/web/party-audio.js"

DEFAULTS = {"theme": "content", "original": 0, "vocal_preset": "studio", "music_gain": 50, "mic_gain": 40, "sfx_gain": 80}
ROOM = {"active": True, "room_id": "SYNTHETIC-room", "simulated": True, "defaults": DEFAULTS, "status": {
    "theme": "content", "microphone_available": True, "mac_online": True, "queue_revision": 4,
    "controls": {**DEFAULTS, "mic_muted": 1, "control_revision": 3},
    "playback": {"online": True, "current": "current", "played": [], "state": "playing", "mic_muted": True,
                 "applied_revision": 3, "safety_latched": False, "vocal_preset": "studio", "vocal_preset_state": "applied",
                 "audio_capabilities": {"vocal_presets": ["dry", "studio", "echo"], "sfx": True}},
    "songs": [{"id": key, "song_id": str(i), "source": "song", "title": title + " · 合成示例", "nickname": "朋友", "state": state,
               "artists": ["合成歌手"], "album": "合成专辑 · 演唱会珍藏纪念版"}
              for i, (key, title, state) in enumerate((("current", "稻香", "ready"), ("next", "晴天", "ready"),
                                                       ("third", "夜空中最亮的星", "ready"), ("fourth", "听妈妈的话", "ready"),
                                                       ("fifth", "搁浅", "separating")), 1)]}}
HOST = {"available": True, "configured": True, "bytes": int(3.4 * 1024**3), "policy": {"enabled": False, "limit_bytes": 20 * 1024**3}}
ENTRIES = [{"id": "song:" + str(i), "title": title + " · 合成示例", "last_used": 1793771000 - i * 8000,
            "source": "song", "artists": ["合成歌手"], "album": "合成专辑 · 演唱会珍藏纪念版",
            **{host: {"bytes": (120 + i * 17 + offset) * 1024**2, "can_delete": i > 3,
                      "reason": "in_queue" if i <= 3 else ""} for host, offset in (("mac", 0), ("nas", 10))}}
           for i, title in enumerate(("稻香", "晴天", "夜空中最亮的星", "小幸运", "平凡之路", "后来", "十年"), 1)]
ENTRIES[4]["mac"] = None
APPS = [{"id": key, "name": name, "description": description, "kind": kind, "players": players, "mode": key}
        for key, name, description, kind, players in (
            ("ski", "林间滑雪", "在雪道间转向，用身体控制速度。", "game", [1, 1]),
            ("tennis", "双人网球", "一人一侧，挥拍接住来球。", "game", [2, 2]),
            ("quest", "体感闯关", "跟着动作，逐关完成身体挑战。", "game", [1, 1]),
            ("werewolf", "狼人杀", "用手机查看身份，一起推理。", "party", [6, 12]))]


def synthetic_cover(identifier):
    """只绘制合成色块，避免预览依赖真实歌曲图片和外网。"""
    color = ((104, 128, 113), (159, 126, 104), (100, 120, 143))[int(identifier) % 3]
    pixels = bytearray()
    for y in range(80):
        pixels.append(0)
        for x in range(80):
            radius = (x - 40) ** 2 + (y - 36) ** 2
            pixels.extend((222, 215, 188) if 125 < radius < 280 else (40, 48, 43) if radius < 625 else color)
    def chunk(name, data):
        return struct.pack(">I", len(data)) + name + data + struct.pack(">I", zlib.crc32(name + data))
    payload = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 80, 80, 8, 2, 0, 0, 0))
    payload += chunk(b"IDAT", zlib.compress(bytes(pixels))) + chunk(b"IEND", b"")
    return "data:image/png;base64," + base64.b64encode(payload).decode()


def handler_for(scenario):
    room = json.loads(json.dumps(ROOM))
    background_policy = {"enabled": True, "daily_limit": 100, "daily_remaining": 98}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, status, body, mime="application/json"):
            self.send_response(status)
            for key, value in (("Content-Type", mime), ("Content-Length", str(len(body))), ("Cache-Control", "no-store")):
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def data(self, value, status=200):
            self.send(status, json.dumps(value, ensure_ascii=False).encode())

        def do_GET(self):
            path = urlsplit(self.path).path
            query = parse_qs(urlsplit(self.path).query)
            if path == "/api/snapshot":
                return self.data(dict(mode="simulator", version="SYNTHETIC-KTV", generated_at="2026-11-04T04:00:00Z",
                                      entities=[], devices=[], states=[], observations=[], events=[], rules=[], limitations=[]))
            if path == "/api/games":
                return self.data({"service": {"configured": False, "owned": False, "capture_enabled": False}, "apps": APPS})
            if path == "/api/embodied/cameras":
                return self.data({"cameras": []})
            if path in {"/api/games/ktv/party-audio.js", "/console/party-audio.js"}:
                return self.send(200, PARTY_AUDIO_FILE.read_bytes(), "text/javascript")
            if path == "/console/icon.png":
                return self.send(200, (STATIC_ROOT / "brand/friday.png").read_bytes(), "image/png")
            prefix = next((p for p in ("/api/games/ktv/", "/v1/") if path.startswith(p)), None)
            if prefix:
                if scenario == "offline":
                    return self.data({"detail": "ktv_offline"}, 503)
                op = path.removeprefix(prefix)
                if op == "manage/artwork":
                    ids = query.get("ids", [""])[0].split(",")
                    return self.data({"covers": {sid: synthetic_cover(sid) for sid in ids}, "tracks": {
                        sid: {"artists": ["合成歌手"], "album": "合成专辑 · 演唱会珍藏纪念版", "source": query.get("source", ["song"])[0]} for sid in ids}})
                if op == "manage/library":
                    target = query.get("selectable", [""])[0]
                    hosts = ("mac", "nas") if target == "both" else (target,)
                    entries = [row for row in ENTRIES if not target or any(row.get(host) for host in hosts) and
                               all(not row.get(host) or row[host]["can_delete"] for host in hosts)]
                    text = query.get("q", [""])[0]
                    entries = [row for row in entries if text in " ".join((row["title"], " ".join(row["artists"]), row["album"]))]
                    offset, limit = int(query.get("offset", ["0"])[0]), int(query.get("limit", ["50"])[0])
                    return self.data({"simulated": True, "mac": HOST, "nas": {**HOST, "bytes": int(6.2 * 1024**3),
                        "policy": {"enabled": True, "limit_bytes": 100 * 1024**3}}, "entries": entries[offset:offset+limit], "total": len(entries)})
                data = {
                    "manage/room": room if scenario == "ready" else {"active": False, "room_id": "", "defaults": DEFAULTS, "status": None},
                    "manage/policy/mac": {**HOST, "hot_cache": {"bytes": HOST["bytes"]}},
                    "manage/policy/nas": {**HOST, "bytes": int(6.2 * 1024**3),
                        "policy": {"enabled": True, "limit_bytes": 100 * 1024**3}},
                    "scheduler": {"hardware": {"cpu": "合成测试主机", "memory_bytes": 16 * 1024**3},
                        "settings": {"mode": "auto", "download": 2, "model": 1, "encode": 2}, "limits": {"download": 3, "model": 2, "encode": 4}},
                    "source/status": {"logged_in": True, "state": "logged_in", "settings": {"level": "exhigh"}}, "jobs": {"jobs": []},
                    "manage/backgrounds": {**background_policy, "items": [
                        {"id": "a" * 64, "title": "稻香 · 合成示例", "source": "song", "status": "ready", "ready": True},
                        {"id": "b" * 64, "title": "晴天 · 合成示例", "source": "cloud", "status": "preparing", "ready": False},
                    ]},
                    "manage/search": {"songs": [{"song_id": str(i), "title": title + " · 合成示例", "artists": ["合成歌手"], "source": "song",
                        "album": "合成专辑 · 演唱会珍藏纪念版", "cached_accompaniment": True}
                        for i, title in enumerate(("稻香", "听妈妈的话", "晴天"), 7)]},
                }
                return self.data(data.get(op, {"detail": "not_found"}), 200 if op in data else 404)
            if path.startswith("/api/"):
                return self.data({"available": False, "configured": False, "status": "not_configured"})
            static = STATIC_ROOT
            relative = "ktv-console/index.html" if path == "/console" else "ktv-console/" + path.removeprefix("/console/") if path.startswith("/console/") else "index.html" if path == "/" else path.removeprefix("/static/")
            file = static / relative
            if (path not in {"/", "/console"} and not path.startswith(("/static/", "/console/"))) or not file.resolve().is_relative_to(static) or not file.is_file():
                return self.data({"detail": "not_found"}, 404)
            return self.send(200, file.read_bytes(), mimetypes.guess_type(file.name)[0] or "application/octet-stream")

        def do_POST(self):
            path = urlsplit(self.path).path
            body = json.loads(self.rfile.read(min(32769, int(self.headers.get("Content-Length", "0")))) or b"{}")
            if path.endswith("/manage/room/songs"):
                value = body.get("value", {})
                identifier = "SYNTHETIC-added-" + str(value.get("song_id", "7"))
                if not any(song["id"] == identifier for song in room["status"]["songs"]):
                    room["status"]["songs"].append({"id": identifier, "song_id": value.get("song_id", "7"),
                        "source": value.get("source", "song"), "title": value.get("title", "合成点歌"),
                        "nickname": "合成访客", "state": "queued"})
                    room["status"]["queue_revision"] += 1
                return self.data({"id": identifier, "accepted": True})
            if path.endswith("/manage/room/order"):
                value = body.get("value", {})
                songs = room["status"]["songs"]
                if body.get("room_id") != room["room_id"] or value.get("revision") != room["status"]["queue_revision"]:
                    return self.data({"detail": "queue_changed"}, 409)
                if set(value.get("ids", [])) != {song["id"] for song in songs}:
                    return self.data({"detail": "invalid_order"}, 400)
                room["status"]["songs"] = [next(song for song in songs if song["id"] == sid) for sid in value["ids"]]
                room["status"]["queue_revision"] += 1
                return self.data({"accepted": True})
            if path.endswith("/manage/preview"):
                return self.data({"plan": "SYNTHETIC-plan", "songs": [{"title": row["title"]} for row in ENTRIES if row["id"] in body.get("ids", [])], "bytes": {"mac": 350 * 1024**2, "nas": 420 * 1024**2}})
            if path.endswith("/manage/backgrounds/policy"):
                if "enabled" in body:
                    background_policy["enabled"] = body["enabled"]
                if "daily_images" in body:
                    background_policy.update(daily_limit=body["daily_images"], daily_remaining=max(0, body["daily_images"] - 2))
                return self.data(background_policy)
            return self.data({"accepted": True, "complete": True, "seq": 5, "revision": 4})
    return Handler


def main():
    global STATIC_ROOT, PARTY_AUDIO_FILE, APPS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=("ready", "empty", "offline"), default="ready")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--candidate", type=Path, help="Read only this generated candidate bundle as static assets")
    args = parser.parse_args()
    if args.candidate is not None:
        candidate = args.candidate.absolute()
        if candidate.resolve() != candidate or not candidate.is_dir() or candidate.suffix != ".app":
            parser.error("canonical_candidate_bundle_required")
        STATIC_ROOT = candidate / "Contents/Resources/product/app/homeplane/static"
        PARTY_AUDIO_FILE = STATIC_ROOT / "ktv-console/party-audio.js"
        APPS = [row for row in APPS if row["id"] in {"ski", "tennis"}]
    with build_workspace(f"python{sys.version_info.major}{sys.version_info.minor}-chrome", "ktv-game-library-preview", configuration="ktv-game-library") as (area, record):
        server = ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(args.scenario))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        record.update(fixture_only=True, scenario=args.scenario, candidate_assets_only=args.candidate is not None)
        print(json.dumps({"url": f"http://127.0.0.1:{server.server_port}/#games", "synthetic": True, "cache_manifest": str(area / "manifest.json")}), flush=True)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)


if __name__ == "__main__":
    main()
