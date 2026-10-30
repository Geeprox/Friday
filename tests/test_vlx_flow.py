import asyncio
import json

import httpx
import pytest

from homeplane.embodied.vlx_flow import (
    PUBLIC_PRESET,
    FlowConfig,
    FlowProtocolError,
    FlowTranscript,
    probe_public_preset,
)


def config():
    return FlowConfig("https://vlx.example/os/", "SYNTHETIC-KEY")


def event(**message):
    return "data: " + json.dumps(message, ensure_ascii=False) + "\n\n"


class Chunks(httpx.AsyncByteStream):
    def __init__(self, content, *, stall=False):
        self.content, self.stall = content, stall

    async def __aiter__(self):
        raw = self.content.encode()
        # Deliberately split both UTF-8 codepoints and SSE lines across packets.
        for i in range(0, len(raw), 7):
            yield raw[i : i + 7]
        if self.stall:
            await asyncio.Event().wait()


def endpoint(content, *, stop_code=0, stall=False, status=200, content_type="text/event-stream"):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("/stop"):
            return httpx.Response(200, json=dict(code=stop_code))
        return httpx.Response(
            status, headers={"content-type": content_type}, stream=Chunks(content, stall=stall)
        )

    return httpx.MockTransport(handler), calls


def test_configuration_does_not_expose_key_or_duplicate_os_prefix():
    assert "SYNTHETIC-KEY" not in repr(config())
    assert config().url("stream") == "https://vlx.example/os/v2/vlx/stream"
    assert FlowConfig("https://vlx.example", "fixture").url("stop") == "https://vlx.example/os/v2/vlx/stop"
    for address in (
        "http://vlx.example",
        "https://user:password@example.invalid",
        "https://vlx.example/?key=private",
    ):
        with pytest.raises(ValueError):
            FlowConfig(address, "fixture")


@pytest.mark.asyncio
async def test_default_never_calls_a_model_and_duration_is_bounded():
    transport, calls = endpoint("")
    with pytest.raises(ValueError, match="explicit"):
        await probe_public_preset(config(), transport=transport)
    with pytest.raises(ValueError, match="duration"):
        await probe_public_preset(config(), execute=True, max_seconds=100, transport=transport)
    assert calls == []


@pytest.mark.asyncio
async def test_fragmented_stream_is_aggregated_by_frame_and_remote_stop_is_explicit():
    data = (
        event(event="taskCreated", taskId="fixture-task", content="control-data")
        + event(event="rtspWsReady", content="https://private.invalid/preview")
        + event(event="delta", frameNo=8, content="拿起")
        + event(event="delta", frameNo=9, content="放下")
        + event(event="delta", frameNo=8, content="杯子")
        + event(event="lineBreak", frameNo=8)
        + event(event="lineBreak", frameNo=9)
        + event(event="done")
    )
    transport, calls = endpoint(data)
    tasks = []
    receipt = await probe_public_preset(config(), execute=True, transport=transport, on_task=tasks.append)
    assert [x["text"] for x in receipt["segments"]] == ["拿起杯子", "放下"]
    assert all(x["source_time"] is None and x["safety_verdict"] == "unreviewed" for x in receipt["segments"])
    assert receipt["stop"] == "accepted" and receipt["transport"] == "test"
    assert tasks == ["fixture-task"]
    assert [c.url.path for c in calls] == ["/os/v2/vlx/stream", "/os/v2/vlx/stop"]
    sent = json.loads(calls[0].content)
    assert sent["source"] == dict(path=PUBLIC_PRESET, type="2")
    assert sent["frame_preempt"] == "queue" and sent["model"] == "vlx-flow"
    assert not receipt["household_media_sent"] and not receipt["device_actions"]
    assert "private.invalid" not in json.dumps(receipt)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["invalid_json", "remote_error", "changed_task", "budget", "incomplete"])
async def test_all_stream_failures_stop_known_task_and_do_not_promote_partial_answer(failure):
    content = event(event="taskCreated", taskId="first") + event(event="delta", frameNo=1, content="有人")
    suffix = {
        "invalid_json": "data: {broken}\n\n",
        "remote_error": event(event="error", content="SYNTHETIC_PRIVATE_ERROR"),
        "changed_task": event(event="taskCreated", taskId="other"),
        "budget": event(event="delta", frameNo=1, content="x" * 5000),
        "incomplete": 'data: {"event":"lineBreak","frameNo":1}',
    }[failure]
    transport, calls = endpoint(content + suffix)
    receipt = await probe_public_preset(config(), execute=True, transport=transport)
    assert receipt["segments"] == []
    assert receipt["stop"] == "accepted"
    assert json.loads(calls[-1].content) == {"taskId": "first"}
    assert "SYNTHETIC_PRIVATE_ERROR" not in json.dumps(receipt)


@pytest.mark.asyncio
async def test_unknown_task_is_unconfirmed_and_refuses_unbounded_streaming():
    transport, calls = endpoint("".join(event(event="delta", frameNo=i, content="x") for i in range(20)))
    result = await probe_public_preset(config(), execute=True, transport=transport)
    assert result["reason"] == "vlx_missing_task_id"
    assert result["stop"] == "unconfirmed_missing_task_id"
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_code", [1, False])
async def test_stop_failure_or_boolean_code_never_means_remote_stop_success(stop_code):
    transport, _ = endpoint(event(event="taskCreated", taskId=12) + event(event="done"), stop_code=stop_code)
    result = await probe_public_preset(config(), execute=True, transport=transport)
    assert result["stop"] == "unconfirmed"


@pytest.mark.asyncio
async def test_timeout_during_idle_sse_still_stops_remote_task():
    transport, calls = endpoint(event(event="taskCreated", taskId="idle"), stall=True)
    result = await probe_public_preset(config(), execute=True, max_seconds=1, transport=transport)
    assert result["reason"] == "time_budget" and result["stop"] == "accepted"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_cancelled_local_connection_also_dispatches_stop():
    seen = asyncio.Event()
    transport, calls = endpoint(event(event="taskCreated", taskId="cancel"), stall=True)
    task = asyncio.create_task(
        probe_public_preset(config(), execute=True, transport=transport, on_task=lambda _: seen.set())
    )
    await seen.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls[-1].url.path.endswith("/stop")


def test_no_caption_may_borrow_frame_reference_or_commit_on_task_done():
    transcript = FlowTranscript()
    with pytest.raises(FlowProtocolError):
        transcript.ingest(dict(event="delta", content="no frame"))
    transcript.ingest(dict(event="delta", frameNo=1, content="partial"))
    transcript.ingest(dict(event="done", frameNo=1))
    assert not transcript.segments
