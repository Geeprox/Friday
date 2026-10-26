import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Anchor(BaseModel):
    camera_id: str
    box: tuple[float, float, float, float]
    description: str = ""

    @model_validator(mode="after")
    def valid_box(self):
        x1, y1, x2, y2 = self.box
        if not (0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1):
            raise ValueError("Anchor must be a normalized [left, top, right, bottom] box")
        return self


class Device(BaseModel):
    id: str
    name: str
    room: str
    aliases: list[str] = Field(default_factory=list)
    entity_id: str | None = None
    did: str | None = None
    power_siid: int | None = None
    power_piid: int | None = None
    brightness_siid: int | None = None
    brightness_piid: int | None = None
    brightness_range: tuple[int, int] = (1, 100)
    anchors: list[Anchor] = Field(default_factory=list)


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["demo", "live"] = "demo"
    wake_mode: Literal["optional", "required"] = "optional"
    backend: Literal["home_assistant", "miloco"] = "miloco"
    camera_id: str = "living-room"
    room: str = "客厅"
    camera_did: str | None = None
    camera_fixed_view: bool = False
    miloco_url: str = "http://127.0.0.1:1810"
    miloco_token_env: str = "FRIDAY_MILOCO_TOKEN"
    ha_url: str = "http://homeassistant.local:8123"
    ha_token_env: str = "FRIDAY_HA_TOKEN"
    asr_backend: Literal["http", "mlx"] = "http"
    asr_url: str = "http://127.0.0.1:8080/v1"
    asr_model: str = "whisper-1"
    asr_token_env: str = "FRIDAY_ASR_TOKEN"
    vlm_url: str = "http://127.0.0.1:1234/v1"
    vlm_model: str = "local-vlm"
    vlm_token_env: str = "FRIDAY_VLM_TOKEN"
    allow_remote_models: bool = False
    attention_threshold: float = Field(default=0.9, ge=0.8, le=1)
    context_seconds: float = Field(default=15, ge=1, le=30)
    max_age_seconds: float = Field(default=12, ge=1, le=30)
    decision_url: str = "http://127.0.0.1:8811"
    decision_model: str = "convaiinnovations/laya-multilingual"
    planner_provider: Literal["arl", "openai", "codex"] = "arl"  # Old values accepted for config migration only.
    arl_home: str | None = None
    arl_planner_provider: Literal["codex_app_server", "ollama"] = "codex_app_server"
    codex_model: str | None = None
    planner_url: str = "http://127.0.0.1:8811/v1"
    planner_model: str = "Qwen/Qwen3.8-27B"
    planner_token_env: str = "FRIDAY_PLANNER_TOKEN"
    model_timeout_seconds: float = Field(default=180, ge=5, le=600)
    plan_ttl_seconds: float = Field(default=60, ge=10, le=180)
    whole_home: bool = True
    devices: list[Device] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_settings(self):
        from urllib.parse import urlparse

        if len({d.id for d in self.devices}) != len(self.devices):
            raise ValueError("Device IDs must be unique")
        for endpoint in (
            self.vlm_url,
            self.asr_url,
            self.miloco_url,
            self.ha_url,
            self.planner_url,
            self.decision_url,
        ):
            u = urlparse(endpoint)
            if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password:
                raise ValueError("Endpoints must be HTTP(S) URLs without embedded credentials")
        if not self.allow_remote_models:
            for endpoint in (self.vlm_url, self.asr_url, self.planner_url, self.decision_url):
                if urlparse(endpoint).hostname not in ("localhost", "127.0.0.1", "::1"):
                    raise ValueError("Remote model endpoints require allow_remote_models=true")
        return self


CONFIG_PATH = Path(os.environ.get("FRIDAY_CONFIG", "friday.local.json"))


def load_settings() -> Settings:
    path = CONFIG_PATH if CONFIG_PATH.exists() else Path("friday.example.json")
    return Settings.model_validate(json.loads(path.read_text()))
