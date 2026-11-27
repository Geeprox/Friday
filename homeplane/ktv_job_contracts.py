"""Mac 制备队列与 NAS 桥接共用的任务契约。"""

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SOURCE_ORIGINS = {"auto": "song", "song": "song", "mv": "mv", "cloud": "cloud", "mv_direct": "mv_direct"}
ACTIVE = {"queued", "acquiring", "separating", "transcoding"}
ERRORS = {
    "cancelled",
    "interrupted",
    "source_unavailable",
    "source_changed",
    "download_unavailable",
    "download_timeout",
    "download_identity_mismatch",
    "download_cancelled",
    "decode_failed",
    "separation_failed",
    "transcode_failed",
    "invalid_audio",
    "storage_full",
    "internal_error",
    "source_login_required",
    "source_rights_denied",
    "source_api_unavailable",
    "source_file_invalid",
}


def requested_audio_origin(preference: str) -> str:
    """Automatic MV matching must never change the selected song's audio edition."""
    return SOURCE_ORIGINS[preference]


def nas_attempt_request_id(job_id: str, attempt: int) -> str:
    """沿用 NAS 队列记录与 Mac 制备记录的确定性绑定。"""
    return job_id if not attempt else str(uuid.uuid5(uuid.UUID(job_id), "retry:" + str(attempt)))


class JobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    song_id: str = Field(pattern=r"^[1-9][0-9]{0,17}$")
    title: str = Field(default="", max_length=120)
    request_id: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    requester: dict | None = None
    cache_only: bool = False
    source_preference: Literal["auto", "mv", "song", "cloud", "mv_direct"] = "auto"
    playback_encoding: Literal["pcm_s16le_wav", "aac_lc_m4a"] = "pcm_s16le_wav"
