"""Opt-in VLX-Flow public-preset probe, isolated from household perception.

Protocol: official Om AI Flow and Python SDK documentation (8994275m0,
9212421m0). Closing SSE is not remote stop. Captions are not safety verdicts.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

FLOW_DOC = "https://s.apifox.cn/46c7cae8-f2d7-4780-a960-e531d6c5eaa8/8994275m0"
# Public coffee example published in the official Flow API documentation.
PUBLIC_PRESET = "https://platform.om-agent.cn/minio/goal/cover/vlxflow-video04.mp4"


class FlowProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class FlowConfig:
    api_endpoint: str
    api_key: str = field(repr=False)

    def __post_init__(self):
        url = urlsplit(self.api_endpoint)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or len(self.api_endpoint) > 512
        ):
            raise ValueError("vlx_https_endpoint_required")
        if not self.api_key or len(self.api_key) > 4096 or any(c in self.api_key for c in "\r\n"):
            raise ValueError("vlx_local_credentials_required")

    def url(self, operation):
        if operation not in {"stream", "stop"}:
            raise ValueError("vlx_operation_not_allowed")
        base = self.api_endpoint.rstrip("/")
        if base.endswith("/os"):
            base = base[:-3]
        return base + "/os/v2/vlx/" + operation


class FlowTranscript:
    """Bounded per-frame aggregation of documented business events.

    Keep frameNo as provider provenance. It is not a source timestamp, and is
    never divided by an assumed frame rate to fabricate an evidence time.
    """

    def __init__(self):
        self.task_id = None
        self.pending = {}
        self.segments = []
        self.terminal = False

    def ingest(self, message):
        if not isinstance(message, dict):
            raise FlowProtocolError("vlx_invalid_message")
        task = message.get("taskId")
        if task is not None:
            if isinstance(task, bool) or not isinstance(task, (str, int)):
                raise FlowProtocolError("vlx_invalid_task")
            task = str(task)
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task):
                raise FlowProtocolError("vlx_invalid_task")
            if self.task_id is not None and self.task_id != task:
                raise FlowProtocolError("vlx_task_changed")
            self.task_id = task
        kind = message.get("event")
        if kind in {"taskCreated", "rtspWsReady"}:
            return  # Control content and media URLs are not model answers.
        if kind == "error":
            raise FlowProtocolError("vlx_remote_error")
        if kind in {"done", "stopped"}:
            self.pending.clear()  # A terminal event does not complete a partial sentence.
            self.terminal = True
            return
        if kind not in {"delta", "lineBreak"}:
            return
        frame = message.get("frameNo")
        if type(frame) is not int or not 0 <= frame <= 2**53:
            raise FlowProtocolError("vlx_frame_reference_required")
        if kind == "delta":
            content = message.get("content", "")
            if not isinstance(content, str) or len(content) > 4096:
                raise FlowProtocolError("vlx_caption_budget")
            if frame not in self.pending and len(self.pending) >= 8:
                raise FlowProtocolError("vlx_frame_budget")
            text = self.pending.get(frame, "") + content
            if len(text) > 4096:
                raise FlowProtocolError("vlx_caption_budget")
            self.pending[frame] = text
        else:
            text = self.pending.pop(frame, "").strip()
            if text:
                if len(self.segments) >= 16:
                    raise FlowProtocolError("vlx_segment_budget")
                self.segments.append(
                    dict(
                        frame_no=frame,
                        text=text,
                        source_time=None,
                        source_time_status="unavailable",
                        safety_verdict="unreviewed",
                    )
                )


async def sse_messages(response):
    """Parse complete SSE messages; token/chunk boundaries are not event boundaries."""
    parts, size, total = [], 0, 0
    async for line in response.aiter_lines():
        size += len(line)
        total += len(line)
        if size > 65536 or total > 262144:
            raise FlowProtocolError("vlx_sse_budget")
        if line == "":
            if parts:
                raw = "\n".join(parts)
                if raw == "[DONE]":
                    return
                try:
                    yield json.loads(raw)
                except json.JSONDecodeError:
                    raise FlowProtocolError("vlx_invalid_json") from None
            parts, size = [], 0
        elif line.startswith("data:"):
            parts.append(line[5:].removeprefix(" "))
    # An incomplete trailing SSE message is not promoted to a finished result.


async def probe_public_preset(
    config: FlowConfig, *, execute=False, max_seconds=15, transport=None, on_task=None
):
    """One bounded public-video request. No upload, RTSP or household media input.

    on_task may persist the private task ID for later cleanup. No credentials,
    raw preview URLs or arbitrary error bodies enter the returned receipt.
    """
    if execute is not True:
        raise ValueError("vlx_explicit_execution_required")
    if type(max_seconds) is not int or not 1 <= max_seconds <= 30:
        raise ValueError("vlx_duration_budget")
    transcript = FlowTranscript()
    receipt = dict(
        model="vlx-flow",
        source="official_public_coffee_preset",
        mode="shadow",
        reason="stream_ended",
        stop="not_started",
        model_output_received=False,
        household_media_sent=False,
        device_actions=False,
        source_time_status="unavailable",
    )
    started, requested = time.monotonic(), False
    cancelled = None
    headers = {"Authorization": "Bearer " + config.api_key}
    async with httpx.AsyncClient(
        transport=transport, trust_env=False, follow_redirects=False, timeout=httpx.Timeout(5, read=8)
    ) as client:
        try:
            async with asyncio.timeout(max_seconds):
                requested = True
                async with client.stream(
                    "POST",
                    config.url("stream"),
                    headers={**headers, "Accept": "text/event-stream"},
                    json=dict(
                        model="vlx-flow",
                        prompt="同步讲解咖啡制作步骤，内容20字以内",
                        system_prompt="只描述可见动作。",
                        interval_seconds=1,
                        frame_preempt="queue",
                        temperature=0.0,
                        max_tokens=40,
                        source=dict(path=PUBLIC_PRESET, type="2"),
                    ),
                ) as response:
                    response.raise_for_status()
                    if not response.headers.get("content-type", "").startswith("text/event-stream"):
                        raise FlowProtocolError("vlx_sse_required")
                    count = 0
                    async for message in sse_messages(response):
                        previous = transcript.task_id
                        transcript.ingest(message)
                        if transcript.task_id and previous is None and on_task:
                            on_task(transcript.task_id)
                        count += 1
                        if transcript.terminal:
                            receipt["reason"] = "remote_terminal_event"
                            break
                        if count >= 256:
                            raise FlowProtocolError("vlx_event_budget")
                        if count >= 4 and transcript.task_id is None:
                            raise FlowProtocolError("vlx_missing_task_id")
        except asyncio.CancelledError as error:
            receipt["reason"] = "cancelled"
            cancelled = error
        except TimeoutError:
            receipt["reason"] = "time_budget"
        except FlowProtocolError as error:
            receipt["reason"] = str(error)
        except httpx.HTTPError:
            receipt["reason"] = "vlx_http_unavailable"
        finally:
            if transcript.task_id is not None:
                receipt["stop"] = "unconfirmed"
                try:
                    # The stop request is independent of the SSE/local deadline.
                    async with asyncio.timeout(6):
                        response = await client.post(
                            config.url("stop"), headers=headers, json={"taskId": transcript.task_id}
                        )
                        response.raise_for_status()
                        value = response.json()
                        if (
                            isinstance(value, dict)
                            and type(value.get("code")) in {int, str}
                            and str(value["code"]) == "0"
                        ):
                            receipt["stop"] = "accepted"
                except (TimeoutError, ValueError, httpx.HTTPError):
                    pass
            elif requested:
                receipt["stop"] = "unconfirmed_missing_task_id"
    receipt.update(
        segments=transcript.segments,
        elapsed_seconds=round(time.monotonic() - started, 3),
        model_output_received=bool(transcript.segments),
        transport="test" if transport is not None else "configured_remote",
    )
    if cancelled is not None:
        raise cancelled
    return receipt
