"""Same-origin game-library access to the existing loopback KTV owner service.

The browser chooses a documented operation, never a destination, credential or
HTTP header. This adapter does not start a service or create a singing session.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import plistlib
import re
import stat
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response

OWNER_ORIGIN = "http://127.0.0.1:8769"
READS = {"manage/room", "manage/library", "manage/search", "jobs", "scheduler", "source/status"}
WRITES = {
    "manage/preview", "manage/delete", "manage/policy", "scheduler", "source/login", "source/poll",
    "source/settings", *{"manage/room/" + op for op in
                        ("theme", "controls", "command", "order", "queue-remove", "songs", "retry", "preferences")},
}
ERRORS = {
    "cache_changed", "archive_changed", "cache_in_use", "plan_expired", "nas_management_unavailable",
    "tv_offline", "room_changed", "queue_changed", "song_playing", "vocal_preset_unavailable",
    "microphone_update_required", "sfx_unavailable", "source_login_required", "source_rights_denied",
    "source_api_unavailable", "source_file_invalid", "download_timeout", "separation_failed",
    "transcode_failed", "storage_full", "search_unavailable", "queue_full", "rate_limited", "invalid_policy",
    "invalid_request", "invalid_control", "invalid_query", "library_unavailable", "source_downloading",
    "source_changed", "download_unavailable", "source_login_unavailable", "source_login_expired",
    "source_settings_invalid", "unauthorized",
}
PRIVATE_FIELDS = {
    "token", "agent_key", "key", "credential", "cookie", "authorization", "endpoint", "origin",
    "media_origin", "root", "path", "source_path", "download_dir", "separation_cache", "headers",
}
MAX_RESPONSE = 4 * 1024 * 1024
LAUNCH_AGENT_LABEL = "local.friday.ktv"


def installed_owner_config(launch_agent: Path, development_root: Path) -> Path:
    """Resolve only the fixed user's installed job; never run its program."""
    try:
        launch_agent = launch_agent.absolute()
        if launch_agent.resolve() != launch_agent:
            raise ValueError
        descriptor = os.open(launch_agent, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                    or info.st_mode & 0o022 or info.st_size > 65536):
                raise ValueError
            job = plistlib.loads(stream.read(65537))
        if not isinstance(job, dict) or job.get("Label") != LAUNCH_AGENT_LABEL:
            raise ValueError
        arguments = job.get("ProgramArguments")
        if (not isinstance(arguments, list) or not 3 <= len(arguments) <= 32
                or any(not isinstance(arg, str) or not arg or len(arg) > 2048
                       or any(ord(char) < 32 for char in arg) for arg in arguments)
                or not Path(arguments[0]).is_absolute()
                or not re.fullmatch(r"python(?:3(?:\.\d+)?)?", Path(arguments[0]).name)
                or not Path(arguments[1]).is_absolute()
                or Path(arguments[1]).name != "ktv_server.py"
                or Path(arguments[1]).parent.name != "scripts" or arguments[2] != "serve"):
            raise ValueError
        flags, index = {}, 3
        valued = {"--config", "--host", "--port", "--download-dir", "--public-config"}
        switches = {"--allow-download", "--launchd-sockets"}
        while index < len(arguments):
            argument = arguments[index]
            flag, separator, inline = argument.partition("=")
            if flag in flags or flag not in valued | switches:
                raise ValueError
            if flag in switches:
                if separator:
                    raise ValueError
                flags[flag] = True
            elif separator:
                if not inline:
                    raise ValueError
                flags[flag] = inline
            else:
                index += 1
                if index >= len(arguments) or arguments[index].startswith("--"):
                    raise ValueError
                flags[flag] = arguments[index]
            index += 1
        if flags.get("--port", "8769") != "8769":
            raise ValueError
        requested = Path(flags.get("--config", "data/ktv/config.json"))
        if ".." in requested.parts:
            raise ValueError
        if not requested.is_absolute():
            directory = job.get("WorkingDirectory")
            if not isinstance(directory, str) or not Path(directory).is_absolute():
                raise ValueError
            working = Path(directory)
            if working.resolve() != working or not working.is_dir() or working.stat().st_uid != os.getuid():
                raise ValueError
            requested = working / requested
        return requested
    except FileNotFoundError:
        # Development has no installed job. A present-but-invalid job must not
        # silently select a different config or credential.
        return development_root / "data/ktv/config.json"
    except (OSError, ValueError, TypeError, AttributeError, plistlib.InvalidFileException, RecursionError):
        raise HTTPException(503, "ktv_config_unavailable") from None


def resolved_owner_token(config_path, launch_agent, development_root):
    path = Path(config_path) if config_path is not None else installed_owner_config(launch_agent, development_root)
    return owner_token(path)


def owner_token(path: Path) -> str:
    """Read only a private regular config, without following symlinks."""
    try:
        path = path.absolute()
        if path.resolve() != path:
            raise ValueError
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 16384):
                raise ValueError
            data = json.loads(stream.read(16385))
        token = data.get("token")
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token):
            raise ValueError
        return token
    except FileNotFoundError:
        raise HTTPException(503, "ktv_not_configured") from None
    except (OSError, ValueError, TypeError, AttributeError):
        raise HTTPException(503, "ktv_config_unavailable") from None


def public_value(value, token):
    if isinstance(value, dict):
        return {key: public_value(item, token) for key, item in value.items()
                if key.lower() not in PRIVATE_FIELDS}
    if isinstance(value, list):
        return [public_value(item, token) for item in value]
    if isinstance(value, str) and token in value:
        return ""
    return value


def create_router(config_path=None, *, transport=None, read_only=False, launch_agent_path=None, development_root=None):
    launch_agent = (Path(launch_agent_path) if launch_agent_path is not None
                    else Path.home() / "Library/LaunchAgents/local.friday.ktv.plist")
    development_root = Path(development_root) if development_root is not None else Path(__file__).resolve().parents[1]

    async def boundary(request: Request):
        try:
            local = bool(request.client and ipaddress.ip_address(request.client.host).is_loopback)
        except ValueError:
            local = False
        if not local:
            raise HTTPException(403, "local_owner_required")
        # A browser can resolve an arbitrary Host to 127.0.0.1. Matching its
        # own Origin would not make that DNS-rebound page a local owner UI.
        host = request.url.hostname or ""
        try:
            local_host = ipaddress.ip_address(host).is_loopback
        except ValueError:
            local_host = host == "localhost"
        if not local_host:
            raise HTTPException(403, "local_owner_required")
        expected = str(request.base_url).rstrip("/")
        origin = request.headers.get("origin")
        if (request.headers.get("sec-fetch-site") == "cross-site"
                or origin is not None and origin != expected
                or request.method == "POST" and origin != expected):
            raise HTTPException(403, "same_origin_required")
        if read_only and request.method == "POST":
            raise HTTPException(403, "ktv_read_only")

    router = APIRouter(prefix="/api/games/ktv", dependencies=[Depends(boundary)])

    @router.get("/party-audio.js")
    async def party_audio():
        source = Path(__file__).resolve().parents[1] / "integrations/ktv_public/web/party-audio.js"
        return Response(source.read_bytes(), media_type="text/javascript", headers={"Cache-Control": "no-store"})

    @router.get("/{operation:path}", operation_id="ktv_owner_read")
    @router.post("/{operation:path}", operation_id="ktv_owner_write")
    async def owner_operation(operation: str, request: Request):
        journal = bool(re.fullmatch(r"jobs/[a-f0-9-]{36}/journal", operation))
        if operation not in (READS if request.method == "GET" else WRITES) and not (journal and request.method == "GET"):
            raise HTTPException(404, "not_found")
        query = dict(request.query_params)
        if len(query) != len(request.query_params.multi_items()):
            raise HTTPException(400, "invalid_query")
        if operation == "manage/search" and request.method == "GET":
            if set(query) - {"q", "source", "offset"}:
                raise HTTPException(400, "invalid_query")
            source, q, offset = query.get("source", "song"), query.get("q", ""), query.get("offset", "0")
            if (source not in {"song", "mv", "cloud"} or not (0 if source == "cloud" else 1) <= len(q.strip()) <= 120
                    or not offset.isascii() or not offset.isdigit() or int(offset) > 100000):
                raise HTTPException(400, "invalid_query")
        elif query:
            raise HTTPException(400, "invalid_query")
        body = None
        if request.method == "POST":
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                raise HTTPException(415, "json_required")
            raw = await request.body()
            if len(raw) > 32768:
                raise HTTPException(413, "request_too_large")
            try:
                body = json.loads(raw)
            except (ValueError, UnicodeError):
                raise HTTPException(400, "invalid_request") from None
            if not isinstance(body, dict):
                raise HTTPException(400, "invalid_request")
        token = await asyncio.to_thread(resolved_owner_token, config_path, launch_agent, development_root)
        try:
            timeout = httpx.Timeout(60 if operation == "manage/delete" else 15, connect=2)
            async with httpx.AsyncClient(base_url=OWNER_ORIGIN, transport=transport, trust_env=False,
                                         follow_redirects=False, timeout=timeout) as client:
                async with client.stream(request.method, "/v1/" + operation, params=query,
                                         json=body if body is not None else None,
                                         headers={"X-KTV-Key": token, "Accept": "application/json"}) as upstream:
                    chunks, length = [], 0
                    async for chunk in upstream.aiter_bytes():
                        length += len(chunk)
                        if length > MAX_RESPONSE:
                            raise HTTPException(502, "ktv_response_unavailable")
                        chunks.append(chunk)
                    try:
                        data = json.loads(b"".join(chunks))
                    except (ValueError, UnicodeError):
                        raise HTTPException(502, "ktv_response_unavailable") from None
                    if not 200 <= upstream.status_code < 300:
                        raw_code = data.get("detail", data.get("code")) if isinstance(data, dict) else None
                        code = raw_code if isinstance(raw_code, str) and raw_code in ERRORS else "ktv_unavailable"
                        status = upstream.status_code if 400 <= upstream.status_code < 600 else 502
                        return JSONResponse({"detail": code}, status_code=status, headers={"Cache-Control": "no-store"})
                    if not isinstance(data, dict):
                        raise HTTPException(502, "ktv_response_unavailable")
                    return JSONResponse(public_value(data, token), headers={"Cache-Control": "no-store"})
        except httpx.HTTPError:
            raise HTTPException(503, "ktv_offline") from None

    return router
