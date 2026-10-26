"""Manage inactive native Miloco profiles. No inference transport or activation.

The family Agent still uses ARL. This bridge only speaks the installed Miloco
2026.9.11 admin contract; cloud credentials remain in Miloco's private config.
"""
from __future__ import annotations

import asyncio
import json
import uuid

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .settings import SettingsService

CATALOG = {
    "mimo-flash": {"label": "MiMo V2.6 Flash · 推荐", "model": "mimo-v2.6-flash",
                   "base_url": "https://api.xiaomimimo.com/v1", "input_cny_per_million": 1,
                   "output_cny_per_million": 2, "media_fit": "native_miloco_adapter",
                   "pricing_url": "https://mimo.mi.com/docs/zh-CN/price/pay-as-you-go"},
    "mimo-pro": {"label": "MiMo V2.6 Pro · 高难度对照", "model": "mimo-v2.6-pro",
                 "base_url": "https://api.xiaomimimo.com/v1", "input_cny_per_million": 3,
                 "output_cny_per_million": 6, "media_fit": "native_miloco_adapter",
                 "pricing_url": "https://mimo.mi.com/docs/zh-CN/price/pay-as-you-go"},
    "glm-flash": {"label": "GLM 5.3 Flash · 适配待验证", "model": "glm-5.3-flash",
                  "base_url": "https://open.bigmodel.cn/api/paas/v4", "input_cny_per_million": 0.8,
                  "output_cny_per_million": 2.8, "media_fit": "audio_and_request_adapter_unverified",
                  "pricing_url": "https://open.bigmodel.cn/pricing"},
    "glm-free": {"label": "GLM 4.6V Flash · 免费视觉对照", "model": "glm-4.6v-flash",
                 "base_url": "https://open.bigmodel.cn/api/paas/v4", "input_cny_per_million": 0,
                 "output_cny_per_million": 0, "media_fit": "audio_and_request_adapter_unverified",
                 "pricing_url": "https://open.bigmodel.cn/pricing"},
    "deepseek-flash": {"label": "DeepSeek Flash · 图像对照", "model": "deepseek-flash",
                       "base_url": "https://api.deepseek.com", "input_cny_per_million": 2,
                       "output_cny_per_million": 8, "off_peak_input_cny_per_million": 1,
                       "off_peak_output_cny_per_million": 4, "media_fit": "images_only_requires_adapter",
                       "pricing_url": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"},
}
PREFIX = "Friday-candidate-"
MAX_PROFILES = 20


class ModelSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preset: str = Field(max_length=32)
    api_key: SecretStr | None = Field(default=None, max_length=4096)


class ModelProbe(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preset: str = Field(max_length=32)


class MilocoModels:
    def __init__(self, settings: SettingsService):
        self.settings = settings
        self.gate = asyncio.Lock()

    async def _request(self, method: str, route: str, body=None) -> dict:
        # SettingsConfig guarantees a credential-free loopback-only origin.
        url = self.settings.config.camera_service_url
        path = self.settings.token_path
        if not url:
            raise ValueError("miloco_not_configured")
        if (path.is_symlink() or any(p.is_symlink() for p in path.parents)
                or path.stat().st_size > 65536 or path.stat().st_mode & 0o077):
            raise ValueError("miloco_invalid_credentials")
        token = json.loads(path.read_text()).get("server", {}).get("token")
        if not isinstance(token, str) or not token or len(token) > 8192:
            raise ValueError("miloco_invalid_credentials")
        async with asyncio.timeout(12):
            async with httpx.AsyncClient(transport=self.settings.transport, timeout=10,
                                         trust_env=False, follow_redirects=False) as client:
                async with client.stream(method, url + "/api/admin/omni-config" + route,
                                         headers={"Authorization": "Bearer " + token}, json=body) as response:
                    if response.status_code != 200:
                        # Never echo upstream error text, headers or partial key masks.
                        raise ValueError("miloco_service_rejected")
                    data = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=16384):
                        data.extend(chunk)
                        if len(data) > 65536:
                            raise ValueError("miloco_response_budget")
        payload = json.loads(data)
        if not isinstance(payload, dict) or payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
            raise ValueError("miloco_bad_response")
        return payload["data"]

    @staticmethod
    def _profile(preset: str, data: dict):
        spec = CATALOG[preset]
        profiles = data.get("profiles", [])
        if not isinstance(profiles, list) or len(profiles) > 256:
            raise ValueError("miloco_bad_response")
        for item in reversed(profiles):
            if (isinstance(item, dict) and isinstance(item.get("label"), str)
                    and item["label"].startswith(PREFIX + preset + "-")
                    and item.get("base_url") == spec["base_url"] and item.get("model") == spec["model"]):
                return item
        return None

    def _public(self, data: dict, status="ready") -> dict:
        active = data.get("active", {})
        if not isinstance(active, dict):
            raise ValueError("miloco_bad_response")
        active_preset = next((key for key, spec in CATALOG.items()
                              if active.get("base_url") == spec["base_url"] and active.get("model") == spec["model"]), None)
        candidates = []
        for preset, spec in CATALOG.items():
            item = self._profile(preset, data)
            candidates.append({"preset": preset, **spec, "saved": item is not None,
                               "key_configured": bool(item and item.get("has_key") is True),
                               "active": bool(item and item.get("label") == active.get("label"))})
        return {"status": status, "catalog": candidates, "pricing_checked_at": "2026-09-30",
                "active_preset": active_preset, "active_key_configured": active.get("has_key") is True,
                "secret_values_returned": False, "activation_supported": False,
                "vision_verified": False, "household_media_sent": False,
                "family_agent_runtime": "ARL", "verification": "NOT_RUN"}

    async def status(self) -> dict:
        try:
            return self._public(await self._request("GET", ""))
        except (ValueError, OSError, TimeoutError, httpx.HTTPError):
            return self._public({}, "service_unavailable")

    async def save(self, selection: ModelSelection) -> dict:
        if selection.preset not in CATALOG:
            raise ValueError("unsupported_model_preset")
        if self.gate.locked():
            return {"status": "busy"}
        async with self.gate:
            spec = CATALOG[selection.preset]
            before = await self._request("GET", "")
            existing = self._profile(selection.preset, before)
            if existing and selection.api_key is None:
                return self._public(before, "already_saved")
            profiles = before.get("profiles", [])
            if not isinstance(profiles, list) or len(profiles) >= MAX_PROFILES:
                return {"status": "profile_capacity_reached"}
            # Every save is a new inactive label. Never edit the active profile:
            # Miloco's PUT can activate an existing active label even with false.
            label = PREFIX + selection.preset + "-" + uuid.uuid4().hex
            key = selection.api_key.get_secret_value() if selection.api_key is not None else ""
            if any(ord(c) < 33 or ord(c) > 126 for c in key):
                raise ValueError("invalid_model_key")
            result = await self._request("PUT", "", {"label": label, "model": spec["model"],
                                                     "base_url": spec["base_url"], "api_key": key,
                                                     "activate": False})
            active_before, active_after = before.get("active", {}), result.get("active", {})
            if (active_before.get("label"), active_before.get("base_url"), active_before.get("model"), active_before.get("has_key")) != (
                    active_after.get("label"), active_after.get("base_url"), active_after.get("model"), active_after.get("has_key")):
                return {"status": "active_configuration_changed", "verification": "unknown_outcome"}
            item = self._profile(selection.preset, result)
            if not item or item.get("label") != label or item.get("has_key") is not bool(key):
                return {"status": "save_unverified", "verification": "unknown_outcome"}
            return self._public(result, "saved_inactive")

    async def probe(self, preset: str) -> dict:
        if preset not in CATALOG:
            raise ValueError("unsupported_model_preset")
        if self.gate.locked():
            return {"status": "busy"}
        async with self.gate:
            data = await self._request("GET", "")
            item = self._profile(preset, data)
            if not item or not item.get("has_key"):
                return {"status": "key_required", "verification": "NOT_RUN"}
            if item.get("label") == data.get("active", {}).get("label"):
                return {"status": "inactive_candidate_required", "verification": "NOT_RUN"}
            # Third-party fused video/audio and request parameters are not proven
            # compatible with Miloco's default MiMo adapter. Don't fake that.
            if CATALOG[preset]["media_fit"] != "native_miloco_adapter":
                return {"status": "media_adapter_required", "verification": "NOT_RUN"}
            result = await self._request("POST", "/test", {"label": item["label"],
                                         "base_url": CATALOG[preset]["base_url"], "model": CATALOG[preset]["model"]})
            ok = result.get("ok") is True and result.get("code") == "ok"
            allowed = {"bad_key", "not_found", "rate_limited", "unreachable", "rejected_authed", "bad_response", "no_key"}
            return {"status": "text_probe_pass" if ok else result.get("code") if result.get("code") in allowed else "probe_failed",
                    "verification": "TEXT_CONNECTIVITY_ONLY" if ok else "FAIL",
                    "vision_verified": False, "household_media_sent": False, "activated": False}
