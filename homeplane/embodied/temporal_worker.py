"""Offline child protocol: bounded JPEGs in, a constrained observation out."""

import argparse
import base64
import gc
import hashlib
import io
import json
import os
import sys
from pathlib import Path

from .temporal_model import MEMORY_LIMIT, WEIGHT_SHA256, VisualResult, model_readiness

PROMPT = (
    "Review these timestamped frames in chronological order. The scene may remain unchanged. "
    "Describe only visible changes. Do not infer age, injury, force, abuse, breathing or intention. "
    "Return one JSON object with exactly two keys: event and observation. "
    "event must be none (ordinary or unchanged scene), unclear (insufficient view), "
    "possible_fall (person visibly transitions from upright to falling), "
    "face_obscured (a face is visibly covered by an object, including persistent covering "
    "when the visible context is sufficient), or rapid_movement "
    "(a person is visibly moved abruptly by another person). "
    "Use none for benign motion or an unchanged safe scene. Use unclear when the target cannot be seen. "
    "observation is a short factual Chinese description, at most 120 characters. "
    "No markdown, no extra keys. This output is for private human review."
)


def main(model):
    os.environ["HF_HUB_OFFLINE"] = os.environ["TRANSFORMERS_OFFLINE"] = "1"
    if not model_readiness(model)["available"]:
        raise ValueError("model_not_ready")
    with (model / "model.safetensors").open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != WEIGHT_SHA256:
            raise ValueError("model_pin_mismatch")
    import mlx.core as mx
    from mlx_vlm import generate, load
    from mlx_vlm.generate.video import timestamped_frame_messages
    from PIL import Image

    mx.set_memory_limit(MEMORY_LIMIT)
    mx.set_cache_limit(64 * 1024**2)
    loaded, processor = load(str(model), trust_remote_code=False, local_files_only=True)
    for line in iter(lambda: sys.stdin.buffer.readline(3 * 1024**2 + 1), b""):
        if len(line) > 3 * 1024**2 or not line.endswith(b"\n"):
            raise ValueError("invalid_window")
        request = json.loads(line)
        if set(request) != {"frames"} or len(request["frames"]) != 4:
            raise ValueError("invalid_window")
        images, stamps = [], []
        for frame in request["frames"]:
            stamp = float(frame["t"])
            if not 0 <= stamp <= 5 or (stamps and stamp <= stamps[-1]):
                raise ValueError("invalid_time_order")
            data = base64.b64decode(frame["jpeg"], validate=True)
            if not 0 < len(data) <= 512 * 1024:
                raise ValueError("invalid_frame")
            with Image.open(io.BytesIO(data)) as image:
                if image.format != "JPEG" or image.width * image.height > 20_000_000:
                    raise ValueError("invalid_frame")
                image = image.convert("RGB")
                image.thumbnail((256, 256))
                images.append(image.copy())
            stamps.append(stamp)
        messages = timestamped_frame_messages(PROMPT, None, 0, stamps)
        prompt = processor.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True, enable_thinking=False
        )
        # Library diagnostics must never enter the line-oriented output protocol.
        from contextlib import redirect_stdout

        with redirect_stdout(sys.stderr):
            response = generate(
                loaded,
                processor,
                prompt,
                image=images,
                max_tokens=128,
                temperature=0,
                enable_thinking=False,
                verbose=False,
            )
        text = response.text.strip()
        if text.startswith("```json\n") and text.endswith("\n```"):
            text = text[8:-4]
        result = VisualResult.model_validate_json(text)
        print(
            json.dumps(dict(version=1, result=result.model_dump(), peak_mlx_bytes=mx.get_peak_memory())),
            flush=True,
        )
        del images, request, response, data, image, frame, line, result, text, prompt, messages, stamps
        gc.collect()
        mx.clear_cache()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    main(parser.parse_args().model)
