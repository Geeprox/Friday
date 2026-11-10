"""Cancellable, bounded HA speech requests. Refresh tokens stay with their owner."""

import asyncio
import hashlib
import json
import math
import time

import httpx

from .speech_lifecycle import SpeechExpired


class AsyncHA:
    def __init__(self, private, *, client_factory=None):
        self.private = private
        self.client_factory = client_factory or httpx.AsyncClient
        self.lock = asyncio.Lock()
        self.cached = None

    def session(self):
        path = self.private() / "ha-session.json"
        stat = path.stat()
        if path.is_symlink() or stat.st_mode & 0o077 or stat.st_size > 65536:
            raise ValueError("speech_session_unavailable")
        raw = path.read_bytes()
        if len(raw) > 65536:
            raise ValueError("speech_session_unavailable")
        value = json.loads(raw)
        if not isinstance(value, dict) or any(
            not isinstance(value.get(key), str) or not value[key] or len(value[key]) > 16384
            for key in ("ha_url", "client_id", "access_token", "refresh_token")
        ):
            raise ValueError("speech_session_unavailable")
        expires = value.get("expires_at")
        if type(expires) not in (int, float) or not math.isfinite(expires):
            raise ValueError("speech_session_unavailable")
        return value, hashlib.sha256(raw).digest()

    async def _json(self, client, method, url, check, **options):
        check()
        async with client.stream(method, url, **options) as response:
            response.raise_for_status()
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                if len(raw) + len(chunk) > 262144:
                    raise ValueError("speech_response_too_large")
                raw.extend(chunk)
        check()
        return json.loads(raw)

    async def request(self, path, data=None, *, guard):
        async with self.lock:
            await guard.checkpoint()
            session, identity = self.session()
            guard.bind("ha_session", identity)
            if self.cached is not None and self.cached[0] != identity:
                self.cached = None

            def check():
                guard.check()
                if self.session()[1] != identity:
                    raise SpeechExpired()

            async with self.client_factory(
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(20, connect=5),
                limits=httpx.Limits(max_connections=1),
            ) as client:
                if self.cached is not None and self.cached[0] == identity:
                    session = {**session, **self.cached[1]}
                if session["expires_at"] < time.time() + 60:
                    fresh = await self._json(
                        client,
                        "POST",
                        session["ha_url"].rstrip("/") + "/auth/token",
                        check,
                        data=dict(
                            grant_type="refresh_token",
                            refresh_token=session["refresh_token"],
                            client_id=session["client_id"],
                        ),
                    )
                    expires = fresh.get("expires_in") if isinstance(fresh, dict) else None
                    if (
                        not isinstance(fresh, dict)
                        or not isinstance(fresh.get("access_token"), str)
                        or not 1 <= len(fresh["access_token"]) <= 16384
                        or type(expires) not in (int, float)
                        or not math.isfinite(expires)
                        or not 0 < expires <= 86400
                    ):
                        raise ValueError("speech_session_unavailable")
                    # A speech job never replaces another subsystem's OAuth file.
                    token = dict(access_token=fresh["access_token"], expires_at=time.time() + expires)
                    self.cached = (identity, token)
                    session = {**session, **token}
                await guard.checkpoint()
                check()
                if data is not None:
                    guard.dispatch()
                return await self._json(
                    client,
                    "GET" if data is None else "POST",
                    session["ha_url"].rstrip("/") + "/api/" + path,
                    check,
                    headers={"Authorization": "Bearer " + session["access_token"]},
                    **({} if data is None else {"json": data}),
                )
