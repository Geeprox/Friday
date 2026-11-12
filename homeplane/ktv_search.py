"""Bounded metadata-only search against the client service's official host, without account cookies."""
from __future__ import annotations

import json
import re
import threading
import time
from collections import OrderedDict

import httpx

from .ktv_download import canonical_song_id

_lock = threading.Lock()
_cache: OrderedDict[str, tuple[float, list[dict]]] = OrderedDict()
_lyrics: OrderedDict[str, dict] = OrderedDict()


def search_songs(query: str) -> list[dict]:
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 120:
        raise ValueError("invalid_query")
    query = query.strip()
    with _lock:
        cached = _cache.get(query)
        if cached and time.monotonic() - cached[0] < 60:
            return [dict(song) for song in cached[1]]
    data = bytearray()
    # Fixed HTTPS host and endpoint; never use a client-supplied URL or forward local credentials.
    with httpx.stream("GET", "https://music.163.com/api/search/get/web",
                      params={"s": query, "type": "1", "limit": "30", "offset": "0"},
                      headers={"Referer": "https://music.163.com/"}, timeout=8, follow_redirects=False) as response:
        if response.status_code != 200:
            raise ValueError("search_unavailable")
        for chunk in response.iter_bytes():
            if len(data) + len(chunk) > 1024 * 1024:
                raise ValueError("search_response_exceeded")
            data.extend(chunk)
    value = json.loads(data)
    if value.get("code") != 200:
        raise ValueError("search_unavailable")
    songs = []
    for item in value.get("result", {}).get("songs", [])[:30]:
        try:
            song_id = canonical_song_id(str(item["id"]))
            title = item["name"]
            if not isinstance(title, str) or not title or len(title) > 120 or any(ord(c) < 32 for c in title):
                continue
            artists = [a["name"][:100] for a in item.get("artists", [])[:8] if isinstance(a.get("name"), str)]
            songs.append({"song_id": song_id, "title": title, "artists": artists,
                          "source": "netease_metadata", "download_permission_confirmed": False})
        except (KeyError, ValueError, TypeError):
            continue
    with _lock:
        _cache[query] = (time.monotonic(), songs)
        _cache.move_to_end(query)
        while len(_cache) > 64:
            _cache.popitem(last=False)
    return [dict(song) for song in songs]


def is_lyric_credit(text: str) -> bool:
    if re.match(
        r"^(?:作\s*[词曲]|[词曲]|编\s*曲|制作(?:人|统筹)?|录音(?:工程(?:师)?|室|师)?|"
        r"混音(?:工程师|录音室|师)?|母带(?:工程师|处理)?|监制|监唱|(?:和|合)声(?:编写)?|"
        r"吉他|钢琴|贝斯|弦乐(?:编写|团)?|编程|女声|男声|演唱|发行|出品|版权|"
        r"Composer|Lyricist|Producer|Publisher|Copyright|OP|SP)\s*[:：]", text, re.IGNORECASE):
        return True
    if re.match(r"^(?:Strings?\s+Arranged(?:\s*&\s*Conducted)?\s+by|"
                r"(?:Vocals?\s+)?Recorded\s+(?:at|by)|Engineered\s+by|"
                r"(?:Mixed|Mastered)\s+(?:at|by))\s+\S", text, re.IGNORECASE):
        return True
    # Imported LRCs also put combined English production roles before a colon.
    # Match the role grammar, rather than treating arbitrary colon text as credits.
    prefix, separator, _ = text.replace("：", ":", 1).partition(":")
    if not separator or len(prefix) > 120:
        return False
    role = (r"(?:Production(?:\s+(?:Coordination|Coordinator))?|Arrangements?|"
            r"(?:String|Vocal)\s+Arrangements?|Keyboards?|Programming|Guitars?|Bass|Drums?|Strings?|Piano|"
            r"(?:(?:Backing|Background|Lead)\s+)?Vocals?|Recording(?:\s+(?:Engineer|Studio))?|"
            r"Mix(?:ing|ed)?(?:\s+Engineer)?|Master(?:ing|ed)?(?:\s+Engineer)?)(?:\s+by)?")
    return re.fullmatch(r"\s*" + role + r"(?:\s*(?:[,/&+]|\band\b)\s*" + role + r")*\s*",
                        prefix, re.IGNORECASE) is not None


def parse_lrc(value: str) -> list[dict]:
    if not isinstance(value, str) or len(value.encode("utf-8")) > 65536:
        raise ValueError("lyrics_budget_exceeded")
    offset_match = re.search(r"\[offset:([+-]?\d{1,5})\]", value)
    offset = int(offset_match.group(1)) if offset_match else 0
    offset = max(-10000, min(offset, 10000))
    lines = []
    for line in value.splitlines()[:1000]:
        stamps = re.findall(r"\[(\d{1,2}):([0-5]\d)(?:[.:](\d{1,3}))?\]", line)
        text = re.sub(r"\[[^\]]*\]", "", line).strip()[:200]
        text = "".join(c for c in text if ord(c) >= 32 and ord(c) != 127)
        if not text:
            continue
        if is_lyric_credit(text):
            continue
        for minute, second, fraction in stamps[:8]:
            milliseconds = int(minute) * 60000 + int(second) * 1000 + int((fraction + "000")[:3]) - offset
            if 0 <= milliseconds <= 1800000 and len(lines) < 1000:
                lines.append({"time_ms": milliseconds, "text": text})
    return sorted(lines, key=lambda row: row["time_ms"])


def song_lyrics(song_id: str) -> dict:
    song_id = canonical_song_id(song_id)
    with _lock:
        if song_id in _lyrics:
            return {"status": "ready", "lines": [dict(row) for row in _lyrics[song_id]["lines"]]}
    data = bytearray()
    with httpx.stream("GET", "https://music.163.com/api/song/lyric",
                      params={"id": song_id, "lv": "1", "kv": "-1", "tv": "-1"},
                      headers={"Referer": "https://music.163.com/"}, timeout=8, follow_redirects=False) as response:
        if response.status_code != 200:
            raise ValueError("lyrics_unavailable")
        for chunk in response.iter_bytes():
            if len(data) + len(chunk) > 256 * 1024:
                raise ValueError("lyrics_budget_exceeded")
            data.extend(chunk)
    value = json.loads(data)
    lines = parse_lrc(value.get("lrc", {}).get("lyric", ""))
    if not lines:
        return {"status": "unavailable", "lines": []}
    result = {"status": "ready", "lines": lines}
    with _lock:
        _lyrics[song_id] = result
        _lyrics.move_to_end(song_id)
        while len(_lyrics) > 64:
            _lyrics.popitem(last=False)
    return result
