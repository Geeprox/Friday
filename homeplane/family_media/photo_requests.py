"""照片界面的任务与结果契约，不接受文件路径或上游地址。"""

from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from homeplane.contracts import Contract

from .contracts import Digest, FamilyMediaOriginalRef, FamilyMediaResolveRequest
from .photo_contracts import PhotoWorkResult


class FamilyMediaPhotoOpen(FamilyMediaResolveRequest):
    request_id: Digest
    expected_original_hash: Digest


class FamilyMediaPhotoRender(Contract):
    request_id: Digest
    expected_context: Digest
    recipe_id: Digest
    purpose: Literal["preview", "export"]
    output_format: Literal["JPEG", "PNG"] = "JPEG"


class FamilyMediaPhotoOutput(Contract):
    context_fingerprint: Digest
    item_id: Digest
    original_ref: FamilyMediaOriginalRef
    recipe_id: Digest | None
    purpose: Literal["preview", "export"]
    result: PhotoWorkResult
    content_url: str = Field(pattern=r"^/api/family-media/photos/content/[a-f0-9]{64}$")
    created_at: AwareDatetime
    expires_at: AwareDatetime
    rendered_derivative: Literal[True] = True
    original_mutated: Literal[False] = False
    saved_to_file: Literal[False] = False
    simulated: bool

    @model_validator(mode="after")
    def qualified_content(self):
        if self.expires_at <= self.created_at:
            raise ValueError("photo_invalid_lifetime")
        if self.purpose == "export" and self.recipe_id is None:
            raise ValueError("photo_export_recipe_required")
        return self


class FamilyMediaPhotoJob(Contract):
    job_id: Digest
    status: Literal["processing", "ready", "failed", "cancelled", "expired"]
    created_at: AwareDatetime
    expires_at: AwareDatetime
    output: FamilyMediaPhotoOutput | None = None
    failure: str | None = None
    simulated: bool

    @model_validator(mode="after")
    def result_matches_state(self):
        if (self.status == "ready") != (self.output is not None):
            raise ValueError("photo_job_result_mismatch")
        if (self.status == "failed") != (self.failure is not None):
            raise ValueError("photo_job_failure_mismatch")
        return self
