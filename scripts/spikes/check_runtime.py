"""Real local model checks using synthetic speech and an empty image, never home media."""

import asyncio
import base64
import io
import json
import time
from pathlib import Path

import httpx
from PIL import Image

from friday.config import Settings
from scripts.spikes.direct_perception import Perception  # Historical experiment only.


async def main():
    settings = Settings(
        asr_url="http://127.0.0.1:8811/v1",
        asr_model="mlx-community/whisper-small-mlx",
        vlm_url="http://127.0.0.1:8811/v1",
        vlm_model="mlx-community/Qwen3.5-4B-4bit",
    )
    results = {}
    async with httpx.AsyncClient(trust_env=False, timeout=120) as client:
        health = (await client.get("http://127.0.0.1:8811/health")).json()
        assert health["status"] == "ready", health
        perception = Perception(settings, client)
        start = time.monotonic()
        text = await perception.transcribe(Path("data/asr-synthetic-test.wav").read_bytes())
        results["asr"] = {"synthetic_text": text, "seconds": round(time.monotonic() - start, 2)}
        assert "客厅" in text and "书房" in text
        results["decision"] = []
        for phrase, expected in [
            ("打开客厅灯", "simple"),
            ("如果客厅超过28度，把空调设为26度，卧室灯保持原样", "complex"),
            ("今天晚饭吃什么", "ignore"),
        ]:
            r = await client.post("http://127.0.0.1:8811/classify", json={"text": phrase})
            r.raise_for_status()
            value = r.json()
            results["decision"].append({"synthetic_text": phrase, "expected": expected, **value})
            print("Laya", json.dumps(results["decision"][-1], ensure_ascii=False), flush=True)
            # Laya confidence is a routing signal, not an accuracy guarantee.
            # Any uncertain result must use the complex planner, never the fast path.
            effective = value["label"] if value["confidence"] >= 0.9 else "complex"
            results["decision"][-1]["effective_route"] = effective
            if expected == "simple":
                assert effective in ("simple", "complex")
            elif expected == "complex":
                assert effective == "complex"
            else:
                assert effective in ("ignore", "complex")
        image = Image.new("RGB", (640, 360), (45, 50, 45))
        output = io.BytesIO()
        image.save(output, format="JPEG")
        encoded = base64.b64encode(output.getvalue()).decode()
        start = time.monotonic()
        evidence = await perception.observe("打开客厅灯", [encoded, encoded])
        results["vision"] = {
            "fixture": "empty image, no humans",
            "evidence": evidence.model_dump(),
            "seconds": round(time.monotonic() - start, 2),
        }
        assert not evidence.live_speaker and evidence.people_count == 0
        print(json.dumps(results, ensure_ascii=False, indent=2))
        Path("data/runtime-verification.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
