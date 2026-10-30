#!/usr/bin/env python3
"""Inspect a local MLX candidate; explicitly run only synthetic temporal inputs.

This is an offline compatibility/latency probe, not an infant safety detector or
a camera worker. It does not download models or activate monitoring/announcements.
"""

import argparse
import importlib.metadata
import json
import os
import platform
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / "data/embodied/models/qwen3.5-2b-4bit"
REVISION = "674aaa7240b91e8012fcad5d791b7dfe5ba90207"
WEIGHT_BYTES = 1722271785
OUTPUT = ROOT / ".artifacts/local-video-review-20261027/synthetic-probe"
PROMPT = (
    "Compare the red circle's horizontal position across these four timestamped frames. "
    "It may move or remain still. Reply with exactly one word: left if it moves left, "
    "right if it moves right, stationary if its position stays the same."
)


def readiness():
    packages = {}
    for name in ("mlx", "mlx-vlm", "transformers"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    return {
        "platform": platform.system(),
        "architecture": platform.machine(),
        "packages": packages,
        "model_present": all((MODEL / f).is_file() for f in ("config.json", "model.safetensors")),
        "expected_model_revision": REVISION,
        "weight_size_matches": (
            (MODEL / "model.safetensors").is_file()
            and (MODEL / "model.safetensors").stat().st_size == WEIGHT_BYTES
        ),
        "mode": "bounded_ordered_frames",
        "native_persistent_video_stream": False,
        "automatic_monitoring": False,
        "network_inference": False,
        "safety_detection_validated": False,
    }


def execute_synthetic(state):
    # Hugging Face and Transformers must resolve everything from the local path.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    import mlx.core as mx
    from mlx_vlm import load, stream_generate
    from mlx_vlm.generate.video import timestamped_frame_messages
    from PIL import Image, ImageDraw

    if not state["model_present"] or not state["weight_size_matches"]:
        raise RuntimeError("local_model_missing; download the pinned checkpoint separately")
    if not mx.metal.is_available():
        raise RuntimeError("Metal unavailable; this probe requires an Apple Silicon runtime")
    mx.set_memory_limit(6 * 1024**3)
    mx.set_cache_limit(256 * 1024**2)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    model, processor = load(str(MODEL), trust_remote_code=False, local_files_only=True)
    load_seconds = time.perf_counter() - started
    cases = []
    for label, positions in (
        ("right", (48, 100, 156, 208)),
        ("left", (208, 156, 100, 48)),
        ("stationary", (128, 128, 128, 128)),
    ):
        paths = []
        for i, x in enumerate(positions):
            image = Image.new("RGB", (256, 256), "white")
            ImageDraw.Draw(image).ellipse((x - 18, 110, x + 18, 146), fill="red")
            path = OUTPUT / f"{label}-{i}.png"
            image.save(path)
            paths.append(str(path))
        messages = timestamped_frame_messages(
            PROMPT,
            None,
            0,
            (0.0, 1.0, 2.0, 3.0),
        )
        prompt = processor.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True, enable_thinking=False
        )
        mx.reset_peak_memory()
        started = time.perf_counter()
        first_token_seconds = None
        chunks = []
        last = None
        deadline_exceeded = False
        generator = stream_generate(
            model,
            processor,
            prompt,
            image=paths,
            max_tokens=24,
            temperature=0.0,
            resize_shape=(256, 256),
            enable_thinking=False,
        )
        try:
            for chunk in generator:
                elapsed = time.perf_counter() - started
                if first_token_seconds is None:
                    first_token_seconds = elapsed
                chunks.append(chunk.text)
                last = chunk
                if elapsed > 90:
                    deadline_exceeded = True
                    break
        finally:
            generator.close()
        answer = "".join(chunks).strip()
        result = {
            "expected": label,
            "answer": answer,
            "exact_match": answer.lower().rstrip(".") == label and not deadline_exceeded,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "first_token_seconds": round(first_token_seconds, 3) if first_token_seconds else None,
            "mlx_peak_gib": round(mx.get_peak_memory() / 1024**3, 3),
            "prompt_tokens": last.prompt_tokens if last else 0,
            "generation_tokens": last.generation_tokens if last else 0,
            "deadline_exceeded": deadline_exceeded,
        }
        cases.append(result)
        print(json.dumps(result), flush=True)
    receipt = dict(
        state,
        input="synthetic_red_circle_only",
        prompt=PROMPT,
        load_seconds=round(load_seconds, 3),
        device=mx.device_info(),
        cases=cases,
        scope="Local inference and simple temporal order only; no household or infant acceptance",
    )
    target = OUTPUT / "receipt.json"
    target.write_text(json.dumps(receipt, indent=2) + "\n")
    print(str(target))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-synthetic", action="store_true")
    args = parser.parse_args()
    state = readiness()
    if args.execute_synthetic:
        execute_synthetic(state)
    else:
        print(json.dumps(state))
