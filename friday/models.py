"""Perception boundary owned by shared ARL; no private model service or downloads."""
import base64
import io
import os
import wave

from homeplane.arl import ARLError


def headers(env: str):
    token = os.environ.get(env)
    return {"Authorization": f"Bearer {token}"} if token else {}


class Perception:
    def __init__(self, settings, client=None, runtime=None):
        self.settings, self.runtime = settings, runtime

    async def transcribe(self, wav: bytes) -> str:
        with wave.open(io.BytesIO(wav)) as audio:
            if audio.getnchannels() != 1 or audio.getsampwidth() != 2 or not 8000 <= audio.getframerate() <= 48000:
                raise ValueError("Expected mono PCM16 WAV")
            if not 0.2 <= audio.getnframes()/audio.getframerate() <= 8:
                raise ValueError("Audio must be between 0.2 and 8 seconds")
        # ARL arl/1 currently has no binary/media/transcription operation. Do not
        # smuggle audio into model.complete or revive a second runtime.
        raise ARLError("arl_asr_contract_unavailable")

    async def observe(self, text: str, frames: list[str]):
        if not 1 <= len(frames) <= 3 or len(text.encode()) > 4096:
            raise ValueError("Invalid observation input budget")
        for frame in frames:
            raw = base64.b64decode(frame, validate=True)
            if not raw.startswith(b"\xff\xd8") or len(raw) > 350_000:
                raise ValueError("Expected bounded JPEG frames")
        raise ARLError("arl_visual_observation_contract_unavailable")
