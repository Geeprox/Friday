import base64
import io
import json
import wave

import httpx
import pytest

from friday.config import Settings
from scripts.spikes.direct_perception import Perception


def wav():
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x00\x00" * 8000)
    return buffer.getvalue()


async def test_local_asr_and_vision_contract():
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path.endswith("transcriptions"):
            assert "multipart/form-data" in request.headers["content-type"]
            return httpx.Response(200, json={"text": "打开客厅灯"})
        body = json.loads(request.content)
        assert body["response_format"]["type"] == "json_object"
        assert body["messages"][1]["content"][1]["type"] == "image_url"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "live_speaker": True,
                                    "people_count": 1,
                                    "facing_camera": True,
                                    "attention": 0.98,
                                }
                            )
                        }
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        perception = Perception(Settings(), client)
        text = await perception.transcribe(wav())
        evidence = await perception.observe(text, [base64.b64encode(b"\xff\xd8mock-jpeg").decode()])
        assert text == "打开客厅灯" and evidence.live_speaker
        assert len(requests) == 2


async def test_model_cannot_return_actions_or_extra_fields():
    response = {"choices": [{"message": {"content": '{"live_speaker":true,"action":"unlock"}'}}]}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=response))
    ) as client:
        with pytest.raises(ValueError):
            await Perception(Settings(), client).observe(
                "打开客厅灯", [base64.b64encode(b"\xff\xd8").decode()]
            )


async def test_invalid_media_fails_before_model_request():
    def unexpected(request):
        pytest.fail("Invalid media reached model endpoint")

    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as client:
        perception = Perception(Settings(), client)
        with pytest.raises((ValueError, wave.Error, EOFError)):
            await perception.transcribe(b"not a wav")
        with pytest.raises(ValueError):
            await perception.observe("打开客厅灯", [base64.b64encode(b"not a jpeg").decode()])
