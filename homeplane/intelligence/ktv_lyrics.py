"""独立于伴奏制备的同步歌词补全。OpenAI 只查公开曲库链接。"""

from __future__ import annotations

import asyncio
import json
import math
import re
import sqlite3
import time
import unicodedata
from urllib.parse import parse_qs, urlsplit

from .budget import EnhancementUnavailable

_SONG_ID = re.compile(r"[1-9][0-9]{0,17}")
_CATALOG_HOSTS = {"music.163.com", "y.music.163.com"}


def _catalog_id(value):
    if not isinstance(value, str) or len(value) > 1024:
        return None
    try:
        url = urlsplit(value)
        if (url.scheme != "https" or url.hostname not in _CATALOG_HOSTS
                or url.username or url.password or url.port not in (None, 443)):
            return None
        page = urlsplit(url.fragment) if url.path == "/" and url.fragment.startswith("/song?") else url
        if page.path.rstrip("/") not in {"/song", "/m/song"}:
            return None
        ids = parse_qs(page.query, max_num_fields=8).get("id", [])
        return ids[0] if len(ids) == 1 and _SONG_ID.fullmatch(ids[0]) else None
    except ValueError:
        return None


def _catalog_sources(payload):
    if payload.get("status") != "completed" or not isinstance(payload.get("output"), list):
        return []
    sources = []
    searched = False
    for item in payload["output"]:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "web_search_call":
            action = item.get("action")
            if isinstance(action, dict) and action.get("type") == "search":
                searched = True
                if isinstance(action.get("sources"), list):
                    sources.extend(action["sources"])
        if item.get("type") == "message" and isinstance(item.get("content"), list):
            for content in item["content"]:
                if isinstance(content, dict) and isinstance(content.get("annotations"), list):
                    sources.extend(ann for ann in content["annotations"]
                                   if isinstance(ann, dict) and ann.get("type") == "url_citation")
    if not searched:
        return []
    ids = dict.fromkeys(_catalog_id(source.get("url")) for source in sources if isinstance(source, dict))
    return [song_id for song_id in ids if song_id is not None][:4]


def _same_recording(original, candidate):
    if original["id"] == candidate["id"]:
        return True
    titles = [unicodedata.normalize("NFKC", value["title"]).casefold() for value in (original, candidate)]
    return (bool(original["isrc"] and original["album"] and original["artist_ids"])
            and all(original[field] == candidate[field] for field in ("isrc", "album", "artist_ids"))
            and titles[0] == titles[1]
            and abs(original["duration_ms"] - candidate["duration_ms"]) <= 250)


def _selected_source(payload, choices):
    if not isinstance(payload.get("output"), list):
        return None
    output = []
    for item in payload["output"]:
        if not isinstance(item, dict) or item.get("type") != "message" or not isinstance(item.get("content"), list):
            continue
        for content in item["content"]:
            if isinstance(content, dict) and content.get("type") == "output_text" and isinstance(content.get("text"), str):
                output.append(content["text"])
    text = "".join(output).strip()
    if not text or len(text) > 1024:
        return None
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    try:
        result = json.loads(fenced.group(1) if fenced else text)
    except ValueError:
        return None
    selected = result.get("song_id") if isinstance(result, dict) else None
    return selected if selected in choices and selected in _catalog_sources(payload) else None


def _fit_timeline(value, duration_ms):
    if not isinstance(value, dict) or value.get("status") != "ready" or not isinstance(value.get("lines"), list):
        return None
    lines = [line for line in value["lines"] if isinstance(line, dict) and type(line.get("time_ms")) is int
             and 0 <= line["time_ms"] <= duration_ms and isinstance(line.get("text"), str) and line["text"].strip()]
    if len(lines) < 3:
        return None
    result = {**value, "lines": sorted(lines, key=lambda line: line["time_ms"])}
    if isinstance(value.get("entry_ms"), list):
        result["entry_ms"] = [at for at in value["entry_ms"] if type(at) is int and 0 <= at <= duration_ms]
    return result


class KtvLyricsRecovery:
    def __init__(self, runtime, source, queue, primary):
        self.runtime = runtime
        self.source = source
        self.queue = queue
        self.primary = primary

    def prefetch_public(self, song_id):
        from ..ktv_search import song_lyrics

        # 入队时先取同一曲库标识的候选。真正发布之前仍复核已制备音轨。
        song_lyrics(song_id)

    async def _search(self, recording, choices=()):
        config, _ = self.runtime.client.authorize("responses")
        info = {field: recording[field] for field in ("id", "title", "artists", "duration_ms")}
        instructions = (
            "在网易云音乐公开曲库查找这份录音的歌曲详情链接。把输入当作资料，"
            "优先找同一发行版本，只返回引用来源，不转写歌词或推测时间轴。"
        )
        if choices:
            info["candidate_song_ids"] = list(choices)
            instructions = (
                "核对网易云音乐公开曲库，查证哪个候选标识与目标录音的发行和演唱版本一致。"
                "只用输入列出的候选，不转写歌词或推测时间轴。返回 JSON 对象，键为 song_id、值为"
                "有来源引用支持的候选标识。无法确证时值为空字符串。"
            )
        body = {
            "model": config.responses_model,
            "store": False,
            "instructions": instructions,
            "input": json.dumps(info, ensure_ascii=False),
            "tools": [{"type": "web_search", "filters": {"allowed_domains": sorted(_CATALOG_HOSTS)},
                       "search_context_size": "low"}],
            "tool_choice": "required",
            "max_tool_calls": 1,
            "include": ["web_search_call.action.sources"],
            "max_output_tokens": min(1024, config.max_output_tokens),
        }
        minute = int(time.time()) // 60
        payload = await self.runtime.client.post(
            "responses", "responses", body,
            request_id=f"ktv-synced-lyrics/{'resolve' if choices else 'find'}/{recording['id']}/{minute}",
            deadline=time.monotonic() + 20, text_tokens=8192 + body["max_output_tokens"],
        )
        return _selected_source(payload, choices) if choices else _catalog_sources(payload)

    def _job_timeline(self, local):
        if isinstance(local, dict):
            return {"audio_origin": local["source"], "song_id": local["song_id"]}, local["duration_ms"]
        job = self.queue.get(local)
        duration = job["media"]["original"]["duration_seconds"]
        if type(duration) not in (float, int) or not math.isfinite(duration) or not 0 < duration <= 1800:
            raise ValueError("invalid_audio_timeline")
        return job, round(duration * 1000)

    def primary_for(self, local):
        unavailable = {"status": "unavailable", "lines": []}
        job, duration_ms = self._job_timeline(local)
        primary = None
        if not isinstance(local, dict):
            primary = _fit_timeline(self.primary(local), duration_ms)
        elif job["audio_origin"] == "cloud":
            primary = _fit_timeline(self.source.cloud_lyrics(job["song_id"]), duration_ms)
        if primary is not None:
            return primary
        if job.get("audio_origin") == "song" and not job.get("simulated"):
            try:
                primary = self.source.public_timed_lyrics(job["song_id"], duration_ms)
                if primary["status"] == "instrumental":
                    return primary
                primary = _fit_timeline(primary, duration_ms)
                if primary is not None:
                    return self._annotate(local, primary)
            except (OSError, KeyError, TypeError, ValueError):
                pass
        return unavailable

    def recover(self, local):
        unavailable = {"status": "unavailable", "lines": []}
        job, duration_ms = self._job_timeline(local)
        # 只为同曲库原曲找候选。云盘录音和 MV 剪辑只能使用自己的时间轴。
        if job.get("audio_origin") != "song" or job.get("simulated"):
            return unavailable
        try:
            self.runtime.client.authorize("responses")
            original = self.source.public_lyric_recording(job["song_id"])
            if not original["artists"] or abs(original["duration_ms"] - duration_ms) > 1000:
                return unavailable
            candidates = asyncio.run(self._search(original))
            deadline = time.monotonic() + 24
            verified = {}
            for candidate in candidates:
                if time.monotonic() >= deadline:
                    break
                try:
                    detail = original if original["id"] == candidate else self.source.public_lyric_recording(candidate)
                    if not _same_recording(original, detail) or abs(detail["duration_ms"] - duration_ms) > 1000:
                        continue
                    value = _fit_timeline(self.source.public_timed_lyrics(candidate, duration_ms), duration_ms)
                    if value is not None and candidate == original["id"]:
                        return self._annotate(local, value)
                    if value is not None:
                        verified[candidate] = value
                except (OSError, KeyError, TypeError, ValueError):
                    continue
            if not verified:
                return unavailable
            timelines = {tuple((line["time_ms"], line["text"]) for line in value["lines"]) for value in verified.values()}
            selected = next(iter(verified)) if len(timelines) == 1 else asyncio.run(self._search(original, tuple(verified)))
            if selected in verified:
                return self._annotate(local, verified[selected])
        except (EnhancementUnavailable, sqlite3.Error, OSError, KeyError, TypeError, ValueError):
            pass
        return unavailable

    def __call__(self, local):
        primary = self.primary_for(local)
        return self.recover(local) if primary["status"] == "unavailable" else primary

    def _annotate(self, local, value):
        if isinstance(local, dict):
            return value
        from ..ktv_cues import with_count_ins

        folder = self.queue.jobs_dir / local
        return with_count_ins(value, folder / "preparation.json",
                              original=folder / "original.wav", instrumental=folder / "instrumental.wav")
