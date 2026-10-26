"""Local perception runtime. Serial Metal inference, in-memory media, replaceable HTTP contracts."""

import asyncio
import base64
import io
import json
import os
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field

QUESTIONS = {
    "route": {
        "type": "choice",
        "instructions": "Classify the user's Chinese smart home utterance. Only an explicit request to control home devices is a command. Select complex for conditions, multiple actions, exclusions or reasoning.",
        "criteria": {
            "simple": "A direct request to turn one named or pointed device on/off or change brightness.",
            "complex": "A home control request involving multiple devices, conditions, room groups, exceptions or reasoning.",
            "ignore": "Everyday conversation, a question, quoted speech, TV audio, narration, or no home control request.",
        },
    }
}


class Runtime:
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="friday-metal")
        self.lock = asyncio.Lock()
        self.status = "loading"
        self.components = {}
        self.models = {}
        self.root = Path(os.environ.get("FRIDAY_MODELS_DIR", "data/models")).resolve()

    async def run(self, function, *args):
        return await asyncio.get_running_loop().run_in_executor(self.executor, function, *args)

    def load(self):
        import laya_mlx
        import mlx.core as mx
        import mlx_whisper
        import numpy as np
        from mlx_vlm import load
        from mlx_vlm.utils import load_config

        # Keep allocations below unified-memory pressure on a 16 GB Mac.
        mx.set_cache_limit(256 * 1024 * 1024)
        for name in ("decision", "vision", "asr"):
            if not (self.root / name / "friday-source.json").exists():
                self.components[name] = "weights_missing"
        if self.components:
            self.status = "weights_missing"
            return
        self.models = {
            name: json.loads((self.root / name / "friday-source.json").read_text())["repository"]
            for name in ("decision", "vision", "asr")
        }
        self.decision = laya_mlx.load(str(self.root / "decision"))
        self.decision.predict("打开客厅灯", QUESTIONS)
        self.components["decision"] = "ready"
        self.vision, self.processor = load(str(self.root / "vision"), trust_remote_code=False)
        self.vision_config = load_config(self.root / "vision")
        self.components["vision"] = "ready"
        mlx_whisper.transcribe(
            np.zeros(16000, dtype=np.float32),
            path_or_hf_repo=str(self.root / "asr"),
            language="zh",
            verbose=None,
        )
        self.components["asr"] = "ready"
        self.status = "ready"

    async def initialize(self):
        try:
            await self.run(self.load)
        except Exception as exc:
            self.status = "error"
            self.components["error_type"] = type(exc).__name__
            # Trace contains no request content at startup.
            import traceback

            traceback.print_exc()

    def classify(self, text):
        answer = self.decision.predict(text, QUESTIONS)["answers"]["route"]
        return {
            "label": answer["choice"],
            "confidence": answer["confidence"],
            "probabilities": answer["probabilities"],
        }

    def transcribe(self, content):
        import mlx_whisper
        import numpy as np

        with wave.open(io.BytesIO(content)) as audio:
            if audio.getnchannels() != 1 or audio.getsampwidth() != 2 or audio.getframerate() != 16000:
                raise ValueError("Expected mono 16 kHz PCM16 WAV")
            if not 0.2 <= audio.getnframes() / 16000 <= 8:
                raise ValueError("Audio duration outside bounds")
            samples = (
                np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2").astype(np.float32) / 32768
            )
        return {
            "text": mlx_whisper.transcribe(
                samples,
                path_or_hf_repo=str(self.root / "asr"),
                language="zh",
                verbose=None,
                condition_on_previous_text=False,
                temperature=0,
            )["text"][:300]
        }

    def observe(self, body):
        from mlx_vlm import generate
        from mlx_vlm.prompt_utils import apply_chat_template
        from mlx_vlm.structured import build_json_schema_logits_processor
        from PIL import Image

        from friday.core import Evidence

        if body.model != self.models["vision"]:
            raise ValueError("Requested vision model is not loaded")
        images, messages = [], []
        for message in body.messages:
            content = message.get("content", "")
            if isinstance(content, str):
                messages.append({"role": message["role"], "content": content})
                continue
            texts = []
            for part in content:
                if part.get("type") == "text":
                    texts.append(part["text"])
                elif part.get("type") == "image_url":
                    url = part["image_url"]["url"]
                    if not url.startswith("data:image/jpeg;base64,"):
                        raise ValueError("Only inline JPEGs accepted")
                    raw = base64.b64decode(url.split(",", 1)[1], validate=True)
                    if len(raw) > 350_000:
                        raise ValueError("Image too large")
                    image = Image.open(io.BytesIO(raw))
                    image.load()
                    if image.width * image.height > 4_000_000:
                        raise ValueError("Image dimensions too large")
                    images.append(image.convert("RGB"))
            messages.append({"role": message["role"], "content": "\n".join(texts)})
        if not 1 <= len(images) <= 5:
            raise ValueError("One to five frames required")
        prompt = apply_chat_template(
            self.processor, self.vision_config, messages, num_images=len(images), enable_thinking=False
        )
        schema = Evidence.model_json_schema()
        schema["required"] = list(schema["properties"])
        tokenizer = getattr(self.processor, "tokenizer", self.processor)
        grammar = build_json_schema_logits_processor(tokenizer, schema)
        result = generate(
            self.vision,
            self.processor,
            prompt,
            image=images,
            max_tokens=300,
            logits_processors=[grammar],
            temperature=0,
            verbose=False,
            resize_shape=(448, 448),
        )
        value = result.text.strip()
        if value.startswith("```"):
            value = value.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        evidence = Evidence.model_validate_json(value, strict=True)
        return {"choices": [{"message": {"role": "assistant", "content": evidence.model_dump_json()}}]}


@asynccontextmanager
async def lifespan(app):
    app.state.runtime = Runtime()
    task = asyncio.create_task(app.state.runtime.initialize())
    yield
    await task
    app.state.runtime.executor.shutdown(wait=True)


app = FastAPI(title="Friday local perception", lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])


@app.middleware("http")
async def bounded_local_input(request, call_next):
    if request.headers.get("origin"):
        return Response("Server-to-server model API", 403)
    if request.method == "POST":
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > 3_000_000:
                return Response("Payload too large", 413)
        request._body = bytes(content)
    return await call_next(request)


@app.get("/v1/models")
async def models(request: Request):
    return {
        "object": "list",
        "data": [{"id": name, "object": "model"} for name in request.app.state.runtime.models.values()],
    }


@app.get("/health")
async def health(request: Request):
    runtime = request.app.state.runtime
    return {
        "status": runtime.status,
        "components": runtime.components,
        "models": runtime.models,
        "busy": runtime.lock.locked(),
    }


class Classification(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class VisionRequest(BaseModel):
    messages: list[dict] = Field(min_length=1, max_length=3)
    model: str = "local-vision"


async def infer(request, function, *args):
    runtime = request.app.state.runtime
    if runtime.status != "ready":
        raise HTTPException(503, "Local models are not ready")
    if runtime.lock.locked():
        raise HTTPException(429, "Local inference is busy")
    async with runtime.lock:
        start = time.monotonic()
        try:
            result = await runtime.run(function, *args)
            result["latency_ms"] = round((time.monotonic() - start) * 1000)
            return result
        except (ValueError, KeyError, TypeError):
            raise HTTPException(422, "Invalid model input or output") from None


@app.post("/classify")
async def classify(body: Classification, request: Request):
    return await infer(request, request.app.state.runtime.classify, body.text)


@app.post("/v1/audio/transcriptions")
async def transcribe(request: Request, file: UploadFile = File()):
    content = await file.read(1_100_001)
    await file.close()
    if len(content) > 1_100_000:
        raise HTTPException(413, "Audio too large")
    return await infer(request, request.app.state.runtime.transcribe, content)


@app.post("/v1/chat/completions")
async def vision(body: VisionRequest, request: Request):
    return await infer(request, request.app.state.runtime.observe, body)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("scripts.spikes.legacy_perception_runtime:app", host="127.0.0.1", port=8811, access_log=False)
