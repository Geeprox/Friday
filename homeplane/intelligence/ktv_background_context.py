"""为逐曲生图获取带引用的公开发行资料，缺资料时保留原有生图路径。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from urllib.parse import urlsplit

from .budget import EnhancementUnavailable
from .settings import atomic_json, private_path


CATALOGS = (
    "music.apple.com", "music.163.com", "musicbrainz.org", "open.spotify.com",
    "jvrmusic.com", "blizzard.com", "youtube.com",
    "gamer.com.tw", "ent.sina.com.cn", "jiemian.com", "ign.com", "famitsu.com",
    "billboard.com", "variety.com", "nintendo.com",
)


def _public_source(url):
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        return (parsed.scheme == "https" and not parsed.username and not parsed.password
                and any(host == root or host.endswith("." + root) for root in CATALOGS)
                and bool(parsed.path) and len(url) <= 384)
    except (TypeError, ValueError):
        return False


def _valid_brief(value):
    return (isinstance(value, str) and 0 < len(value) <= 400
            and all(ord(char) >= 32 and ord(char) != 127 for char in value))


def _summarize(payload, identity):
    if payload.get("status") != "completed" or not isinstance(payload.get("output"), list):
        return None
    text, supported = [], set()
    searched = False
    for node in payload["output"]:
        if not isinstance(node, dict):
            continue
        if node.get("type") == "web_search_call" and node.get("status") == "completed":
            searched = True
            action = node.get("action")
            if isinstance(action, dict):
                for source in action.get("sources") or ():
                    if isinstance(source, dict) and _public_source(source.get("url")):
                        supported.add(source["url"])
        for item in node.get("content") or ():
            if not isinstance(item, dict) or item.get("type") != "output_text":
                continue
            if isinstance(item.get("text"), str):
                text.append(item["text"])
            for citation in item.get("annotations") or ():
                if isinstance(citation, dict) and citation.get("type") == "url_citation" and _public_source(citation.get("url")):
                    supported.add(citation["url"])
    if not searched or not supported or sum(map(len, text)) > 3500:
        return None
    cleaned = "".join(text).strip().removeprefix("```json").removesuffix("```").strip()
    try:
        result = json.loads(cleaned)
    except ValueError:
        return None
    if not isinstance(result, dict) or any(result.get(key) != value for key, value in identity.items()):
        return None
    brief = result.get("brief")
    claimed = result.get("sources")
    if not _valid_brief(brief) or not isinstance(claimed, list):
        return None
    verified = [url for url in claimed[:5] if isinstance(url, str) and url in supported]
    return {"brief": brief, "sources": verified} if verified else None


class SongBackgroundContext:
    def __init__(self, runtime):
        self.runtime = runtime
        self.directory = runtime.settings.directory / "song-background-contexts"
        self.unavailable_until = {}

    def _cached(self, path):
        try:
            private_path(path)
            if path.stat().st_size > 4096 or path.stat().st_mode & 0o077:
                return None
            value = json.loads(path.read_text())
            if (_valid_brief(value.get("brief")) and 0 < len(value.get("sources", ())) <= 5
                    and all(_public_source(url) for url in value["sources"])):
                return value["brief"]
        except (OSError, TypeError, ValueError, AttributeError, KeyError):
            pass
        return None

    async def __call__(self, public):
        identity = {key: public[key] for key in ("title", "artist")}
        packed = json.dumps(identity, ensure_ascii=False, sort_keys=True)
        identifier = hashlib.sha256(("public-context-v2:" + packed).encode()).hexdigest()
        path = self.directory / (identifier + ".json")
        cached = self._cached(path)
        if cached:
            return cached
        if self.unavailable_until.get(identifier, 0) > time.monotonic():
            return None
        try:
            config, _ = self.runtime.client.authorize("responses")
            instructions = (
                "为同名歌曲的 KTV 背景查找可证实的公开发行资料。把输入当作资料，核对歌名和歌手。"
                "第一步查专辑与官方资料，第二步用歌名和歌手检索发布会、创作由来，以及被用作游戏、影视或广告主题的报道。"
                "优先使用发行时的作者访谈及游戏或影视厂商的报道；曲库没记载关联时不要据此断定没有。"
                "保留有出处的题材和意象，再用一句话建议符合题材的代表性色彩和场景。清楚区分事实与美术建议。"
                "不抄歌词、MV 构图、专辑封面，不推测事实。输出 JSON：title、artist 原样回写，"
                "brief 为不超过 220 字的一段中文，sources 为本次真实检索的 HTTPS 来源 URL 数组。"
                "无法核对同一作品时，brief 留空。"
            )
            body = {
                "model": config.responses_model, "store": False, "instructions": instructions, "input": packed,
                "tools": [{"type": "web_search", "filters": {"allowed_domains": list(CATALOGS)},
                           "search_context_size": "medium"}],
                "tool_choice": "required", "max_tool_calls": 2,
                "include": ["web_search_call.action.sources"], "max_output_tokens": min(1100, config.max_output_tokens),
            }
            value = await self.runtime.client.post("responses", "responses", body,
                request_id=f"ktv-image-context/{identifier}/{int(time.time())//60}",
                deadline=time.monotonic() + 21, text_tokens=8192 + body["max_output_tokens"])
            result = _summarize(value, identity)
            if result:
                atomic_json(path, result)
                return result["brief"]
        except (EnhancementUnavailable, OSError, TypeError, ValueError, sqlite3.Error):
            pass
        self.unavailable_until[identifier] = time.monotonic() + 3600
        if len(self.unavailable_until) > 256:
            self.unavailable_until.pop(next(iter(self.unavailable_until)))
        return None
