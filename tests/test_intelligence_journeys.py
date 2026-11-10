"""使用真实控制面与合成 API 回执，验证增强失败不改变本地权限。"""

import asyncio
import base64
import io
import json
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from homeplane.api import create_app
from homeplane.intelligence.backgrounds import SongBackgrounds
from homeplane.intelligence.budget import EnhancementUnavailable
from homeplane.intelligence.voice import MAX_SESSION_SECONDS, RealtimeVoice
from tests.test_homeplane_home_flow import domain
from tests.test_homeplane_home_questions import ask, make_adapter
from tests.test_household_timezone_wiring import OfflineARL
from tests.test_intelligence_providers import completion, runtime


def picture():
    output = io.BytesIO()
    Image.new("RGB", (768, 512), (24, 40, 60)).save(output, format="PNG")
    return output.getvalue()


async def test_real_question_reads_original_values_after_api_interpretation_and_local_grammar_works_at_zero_quota(
    tmp_path,
):
    calls = []

    def handler(incoming):
        calls.append(json.loads(incoming.content))
        return httpx.Response(
            200, json=completion(json.dumps(dict(kind="state", rooms=["客厅"], capabilities=["temperature"])))
        )

    owner = runtime(tmp_path / "ai", handler)
    app = create_app(
        make_adapter(tmp_path),
        db_path=tmp_path / "trace.sqlite",
        settings_path=tmp_path / "settings.json",
        arl_runtime=OfflineARL(),
        intelligence_runtime=owner,
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Origin": "http://testserver"},
        ) as client:
            _, report = await ask(client, "客厅的温度能查一下吗？")
            assert report["phase"] == "result", report
            result = domain(report)
            assert result["interpretation"] == dict(provider="openai_api", read_only=True)
            assert any(item["value"] == 24.5 for item in result["query"]["items"])
            assert "24.5" not in calls[0]["input"]
            owner.settings.update({"daily_requests": 0})
            _, local = await ask(client, "客厅温度多少", operation="question:two")
            assert local["phase"] == "result" and len(calls) == 1
            assert (
                app.state.core.store.connection.execute("SELECT count(*) FROM light_actions").fetchone()[0]
                == 0
            )


def test_settings_are_same_origin_masked_and_do_not_make_billable_requests(tmp_path):
    calls = []
    owner = runtime(tmp_path / "ai", lambda request: calls.append(request))
    app = create_app(
        db_path=tmp_path / "trace.sqlite",
        settings_path=tmp_path / "settings.json",
        arl_runtime=OfflineARL(),
        intelligence_runtime=owner,
    )
    with TestClient(app, base_url="http://127.0.0.1:8740", client=("127.0.0.1", 47000)) as client:
        endpoint = "/api/settings/intelligence"
        assert (
            client.put(
                endpoint, json={"daily_requests": 1}, headers={"origin": "https://evil.invalid"}
            ).status_code
            == 403
        )
        saved = client.put(endpoint, json={"daily_requests": 2}, headers={"origin": "http://127.0.0.1:8740"})
        assert saved.status_code == 200 and saved.json()["config"]["daily_requests"] == 2
        secret = client.post(
            endpoint + "/credential",
            json={"api_key": "SIMULATED-private-key"},
            headers={"origin": "http://127.0.0.1:8740"},
        )
        assert secret.status_code == 200 and "SIMULATED" not in secret.text
        invalid = client.post(
            endpoint + "/credential", json={"api_key": "private"}, headers={"origin": "http://127.0.0.1:8740"}
        )
        assert invalid.status_code == 503 and "private" not in invalid.text
        assert not calls and client.get("/api/snapshot").status_code == 200
        assert (
            client.put(
                endpoint, json={"daily_requests": True}, headers={"origin": "http://127.0.0.1:8740"}
            ).status_code
            == 422
        )
        assert (
            client.post(endpoint + "/retry", json={}, headers={"origin": "http://127.0.0.1:8740"}).status_code
            == 200
        )
        assert not calls


async def test_background_is_nonblocking_deduplicated_reusable_and_uses_only_song_metadata(tmp_path):
    began, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def handler(request):
        calls.append(json.loads(request.content))
        began.set()
        await release.wait()
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(picture()).decode()}]})

    owner = runtime(tmp_path / "ai", handler)
    owner.settings.update({"cloud_images": True})
    images = SongBackgrounds(owner)
    song = {
        "song_id": "42",
        "title": "合成歌曲",
        "artist": "合成演唱者",
        "requester": {"name": "PRIVATE-not-for-cloud"},
    }
    try:
        first = images.request(song)
        assert first["status"] == "preparing" and first["kind"] == "local"
        await began.wait()
        assert images.request(song)["status"] == "preparing" and len(calls) == 1
        assert "PRIVATE" not in json.dumps(calls) and calls[0]["n"] == 1
        release.set()
        await asyncio.gather(*list(images.tasks.values()))
        ready = images.request(song)
        assert ready["status"] == "ready"
        assert SongBackgrounds(owner).request(song) == ready
        owner.settings.update({"daily_requests": 0, "cloud_images": False})
        assert images.request(song) == ready and len(calls) == 1
        assert owner.status()["usage"]["used"]["images"] == 1
    finally:
        await images.close()


async def test_background_quota_and_bad_image_never_replace_local_fallback(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(b"not a picture").decode()}]})

    owner = runtime(tmp_path / "ai", handler)
    owner.settings.update({"cloud_images": True, "daily_images": 0})
    images = SongBackgrounds(owner)
    try:
        song = {"song_id": "42", "title": "合成歌曲"}
        images.request(song)
        await asyncio.gather(*list(images.tasks.values()))
        assert images.request(song)["kind"] == "local" and not calls
        owner.settings.update({"daily_images": 1})
        images.request(song)
        await asyncio.gather(*list(images.tasks.values()))
        assert images.request(song)["kind"] == "local" and len(calls) == 1
        assert not list(images.directory.glob("*.png"))
    finally:
        await images.close()


class FakeSocket:
    def __init__(self):
        self.sent = []
        self.events = iter(
            [
                {"type": "response.output_text.delta", "delta": "客厅温度多少"},
                {"type": "response.done", "response": {"status": "completed"}},
            ]
        )

    async def send(self, value):
        self.sent.append(json.loads(value))

    async def recv(self):
        return json.dumps({"type": "session.updated"})

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return json.dumps(next(self.events))
        except StopIteration:
            raise StopAsyncIteration


async def test_realtime_uses_server_key_no_tools_bounded_audio_and_separate_budget(tmp_path):
    owner = runtime(tmp_path / "ai", lambda request: None)
    owner.settings.update({"cloud_voice": True})
    socket = FakeSocket()
    closed = []

    @asynccontextmanager
    async def connector(url, **kwargs):
        assert url == "wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1"
        assert kwargs["proxy"] is None and "Authorization" in kwargs["additional_headers"]
        try:
            yield socket
        finally:
            closed.append(True)

    async def audio():
        yield b"\0\0" * 24000

    events = []

    async def emit(value):
        events.append(value)

    await RealtimeVoice(owner, connector=connector).transcribe(audio(), emit)
    assert socket.sent[0]["session"]["tools"] == []
    assert socket.sent[0]["session"]["output_modalities"] == ["text"]
    assert events[-1] == {"event": "transcript", "text": "客厅温度多少", "device_authority": False}
    assert "SIMULATED" not in json.dumps(events)
    assert 0 < owner.status()["usage"]["used"]["voice_seconds"] <= MAX_SESSION_SECONDS and closed
    owner.settings.update({"daily_voice_seconds": 0})
    with pytest.raises(EnhancementUnavailable, match="voice_seconds"):
        await RealtimeVoice(owner, connector=connector).transcribe(audio(), emit)
    assert len(closed) == 1
