"""Worker question data; no client fields can create a query grant or effect."""
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from homeplane.home_flow_contracts import FlowResponse


class VoiceQuestion(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)
    request_id: str = Field(pattern=r'^[a-f0-9]{64}$')
    camera: str = Field(pattern=r'^cam_[a-f0-9]{10}$')
    camera_source_context: str = Field(pattern=r'^[a-f0-9]{64}$')
    text: str = Field(min_length=1, max_length=1000)
    attending: bool
    after_wake: bool = False
    captured_mono: float = Field(ge=0)
    voice_revision: int = Field(ge=0)

    @field_validator('text')
    @classmethod
    def bounded_text(cls, value):
        if len(value.encode())>4096 or any(ord(c)<32 or ord(c)==127 for c in value):
            raise ValueError('invalid_voice_question')
        return value


class VoiceAnswer(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    request_id: str = Field(pattern=r'^[a-f0-9]{64}$')
    camera: str = Field(pattern=r'^cam_[a-f0-9]{10}$')
    camera_source_context: str = Field(pattern=r'^[a-f0-9]{64}$')
    voice_revision: int = Field(ge=0, strict=True)
    read_only: Literal[True] = True
    device_authority: Literal[False] = False
    reply: str = Field(min_length=1, max_length=1200)
    valid_until: AwareDatetime
    report: FlowResponse
