#!/usr/bin/env python3
"""Authorized read-only bounded packet probe. No decode, ASR, persistence or device writes."""
import argparse
import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx
import websockets


async def probe(config_path, token_path, allow_camera, seconds):
    settings = json.loads(Path(config_path).read_text())
    token = os.environ.get(settings.get("miloco_token_env", "FRIDAY_MILOCO_TOKEN"))
    if not token and Path(token_path).is_file():
        token = json.loads(Path(token_path).read_text()).get("server", {}).get("token")
    result = {"simulated": False, "observed_at": datetime.now(timezone.utc).isoformat(),
              "authorized_read": True, "media_persisted": False, "decoded": False,
              "model_invoked": False, "device_write": False,
              "token_available": bool(token), "streams": {}}
    if not token:
        result["status"] = "BLOCKED_NO_CREDENTIAL"
        return result
    headers = {"Authorization": "Bearer " + token}
    base = settings["miloco_url"].rstrip("/")
    async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
        try:
            response = await client.get(base + "/api/miot/status", headers=headers)
            result["http_status"] = response.status_code
            if response.status_code == 200:
                body = response.json()
                result["api_code"] = body.get("code")
                data = body.get("data", {})
                if isinstance(data, dict):
                    result["account_bound"] = data.get("is_bound")
                    result["max_enabled_cameras"] = data.get("max_enabled_cameras")
                result["status_data_keys"] = sorted(body.get("data", {})) if isinstance(body.get("data"), dict) else []
        except Exception as exc:
            result["status_error"] = type(exc).__name__
    if not allow_camera or not settings.get("camera_did"):
        result["stream_status"] = "NOT_REQUESTED_OR_NO_CAMERA"
        return result
    ws_base = base.replace("http:", "ws:", 1).replace("https:", "wss:", 1)

    async def read_stream(kind):
        report = {"binary_packets": 0, "binary_bytes": 0, "init_received": False,
                  "connected": False, "error_received": False, "duration_limit_seconds": seconds}
        started = time.monotonic()
        query = urlencode({"camera_id": settings["camera_did"], "channel": 0})
        try:
            async with asyncio.timeout(seconds):
                async with websockets.connect(ws_base + "/api/miot/ws/" + kind + "_stream?" + query,
                        additional_headers=headers, proxy=None, open_timeout=5,
                        close_timeout=1, max_queue=2, max_size=2_000_000) as stream:
                    report["connected"] = True
                    async for packet in stream:
                        if isinstance(packet, bytes):
                            report["binary_packets"] += 1
                            report["binary_bytes"] += len(packet)
                            if kind == "video" and len(packet) > 16:
                                report["video_header_present"] = True
                            if report["binary_bytes"] > 12_000_000:
                                report["byte_limit_reached"] = True
                                break
                        else:
                            meta = json.loads(packet)
                            if meta.get("type") == "init":
                                report["init_received"] = True
                                for key in ("codec", "container", "sampleRate", "numberOfChannels"):
                                    if key in meta:
                                        value = meta[key]
                                        if isinstance(value, (int, float)) or (isinstance(value, str) and len(value) <= 32):
                                            report[key] = value
                            if meta.get("type") == "error":
                                report["error_received"] = True
                                break
        except TimeoutError:
            report["duration_limit_reached"] = True
        except Exception as exc:
            report["exception_class"] = type(exc).__name__
        report["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
        report["status"] = "PASS_PACKET_RECEIVE" if report["binary_packets"] else "INCONCLUSIVE_NO_PACKETS"
        return kind, report
    result["streams"] = dict(await asyncio.gather(read_stream("video"), read_stream("audio")))
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="friday.home.json")
    parser.add_argument("--token-config", default="data/miloco/config.json")
    parser.add_argument("--allow-camera", action="store_true")
    parser.add_argument("--seconds", type=float, default=10)
    parser.add_argument("--output", default="docs/evidence/camera-read-probe.json")
    args = parser.parse_args()
    if not 0 < args.seconds <= 10:
        parser.error("duration must be between 0 and 10 seconds")
    result = asyncio.run(probe(args.config, args.token_config, args.allow_camera, args.seconds))
    Path(args.output).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
