"""在调用点选择通道；API 回执和 ARL 原生回执保持不同。"""

from __future__ import annotations

import asyncio
import sqlite3
import time

from homeplane.arl import ARLError

from .budget import EnhancementUnavailable, UsageLedger
from .openai import OpenAIClient
from .settings import IntelligenceSettings


def verified_planning_result(result, request_id=None, scenario="home_planning"):
    if (
        not isinstance(result, dict)
        or result.get("simulated") is not False
        or result.get("device_authority", False if scenario == "voice_intent" else None) is not False
    ):
        return False
    if result.get("provider_id") == "openai_responses":
        receipt = result.get("api_receipt", {})
        return (
            isinstance(receipt, dict)
            and result.get("device_authority") is False
            and receipt.get("status") == "completed"
            and receipt.get("scenario_id") == scenario
            and receipt.get("response_received") is True
            and receipt.get("usage_reserved") is True
            and isinstance(result.get("request_id"), str)
            and (request_id is None or result["request_id"] == request_id)
        )
    return (
        result.get("native_acknowledgements_verified") is True
        and isinstance(result.get("application_admission"), dict)
        and result.get("application_admission", {}).get("scenario_id") == scenario
        and result.get("application_admission", {}).get("package_id")
        == ("low" if scenario == "voice_intent" else "high")
    )


class IntelligenceRuntime:
    def __init__(self, arl, directory, *, transport=None, key_resolver=None, now=None):
        self.arl = arl
        self.settings = IntelligenceSettings(directory, key_resolver=key_resolver)
        self._ledger, self._client = None, None
        self._now, self._transport = now, transport
        self.last_route = None

    @property
    def ledger(self):
        if self._ledger is None:
            self._ledger = UsageLedger(self.settings.directory / "intelligence-usage.sqlite", now=self._now)
        return self._ledger

    @property
    def client(self):
        if self._client is None:
            self._client = OpenAIClient(self.settings, self.ledger, transport=self._transport)
        return self._client

    def status(self):
        config = self.settings.read()
        try:
            credential = bool(self.settings.key())
        except (ValueError, OSError):
            credential = False
        return {
            "config": config.model_dump(),
            "api_key_configured": credential,
            "usage": self.ledger.status(config),
            "last_route": self.last_route,
            "billing_balance": "not_available",
            "device_authority": False,
        }

    def allows_arl_analysis(self):
        try:
            config = self.settings.read()
            return (
                config.provider == "arl"
                or config.provider in {"openai", "auto"}
                and config.allow_arl_fallback
            )
        except (ValueError, OSError):
            return False

    async def complete_scenario(self, scenario, prompt, *, request_id, deadline):
        try:
            config = self.settings.read()
        except (ValueError, OSError):
            raise ARLError("intelligence_settings_unavailable") from None
        if scenario not in {"home_planning", "voice_intent"}:
            raise ARLError("intelligence_scenario_unsupported")
        if config.provider == "local":
            raise ARLError("intelligence_local_only")
        if config.provider == "arl":
            if not callable(getattr(self.arl, "complete_scenario", None)):
                raise ARLError("intelligence_arl_unavailable")
            return await self.arl.complete_scenario(
                scenario, prompt, request_id=request_id, deadline=deadline
            )
        try:
            client = await asyncio.to_thread(lambda: self.client)
            remaining = deadline - time.monotonic()
            api_deadline = deadline
            if config.allow_arl_fallback and callable(getattr(self.arl, "complete_scenario", None)):
                api_deadline -= min(5, max(0, remaining) / 4)
            result = await client.complete(
                prompt, request_id=request_id, deadline=api_deadline, scenario=scenario
            )
            self.last_route = {"provider": "openai", "reason": None}
            return result
        except (EnhancementUnavailable, sqlite3.Error, OSError, ValueError) as exc:
            code = exc.code if isinstance(exc, EnhancementUnavailable) else "usage_ledger_unavailable"
            self.last_route = {"provider": "local", "reason": code}
            try:
                current = self.settings.read() == config
            except (ValueError, OSError):
                current = False
            if (
                not config.allow_arl_fallback
                or time.monotonic() >= deadline
                or code in {"intelligence_configuration_changed", "request_already_attempted"}
                or not current
                or not callable(getattr(self.arl, "complete_scenario", None))
            ):
                raise ARLError("intelligence_" + code) from None
            try:
                result = await self.arl.complete_scenario(
                    scenario, prompt, request_id=request_id, deadline=deadline
                )
                self.last_route = {"provider": "arl", "reason": code}
                return result
            except (ARLError, TimeoutError, OSError):
                raise ARLError("intelligence_" + code) from None
