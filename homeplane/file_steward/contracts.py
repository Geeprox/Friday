"""Preview contracts; source/content identity stays in family_media."""

import re
import unicodedata
from typing import Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator

from homeplane.contracts import Contract
from homeplane.family_media.contracts import Digest, FamilyMediaOriginalRef, OpaqueRef

from .policy import StewardPolicyResolution


def relative_path(value: str) -> str:
    value = unicodedata.normalize("NFC", value)
    parts = value.split("/")
    reserved = r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)"
    if (
        len(value.encode()) > 240
        or len(parts) > 5
        or any(
            not p
            or p in {".", ".."}
            or p.endswith((" ", "."))
            or len(p.encode()) > 120
            or re.search(r'[\x00-\x1f\x7f\\:*?"<>|]', p)
            or re.match(reserved, p, re.I)
            for p in parts
        )
    ):
        raise ValueError("steward_invalid_relative_path")
    return value


class StewardPageRequest(Contract):
    expected_context: Digest
    cursor: Digest | None = None
    limit: int = Field(default=50, ge=1, le=100)
    query: str = Field(default="", max_length=120)
    search_mode: Literal["catalog_page", "indexed_cache"] = "catalog_page"


class StewardSourceAnchor(Contract):
    content_id: Digest
    original_ref: FamilyMediaOriginalRef
    kind: Literal["file", "page", "image_region", "video_time", "audio_time"]
    page: int | None = Field(default=None, ge=1, le=100000)
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=0)
    region: tuple[float, float, float, float] | None = None

    @model_validator(mode="after")
    def validate_anchor(self):
        if (self.kind == "page") != (self.page is not None):
            raise ValueError("steward_page_reference_required")
        timed = self.kind in {"video_time", "audio_time"}
        if timed != (self.start_ms is not None and self.end_ms is not None):
            raise ValueError("steward_time_reference_required")
        if not timed and (self.start_ms is not None or self.end_ms is not None):
            raise ValueError("steward_unexpected_time_reference")
        if timed and self.end_ms <= self.start_ms:
            raise ValueError("steward_invalid_time_range")
        if (self.kind == "image_region") != (self.region is not None):
            raise ValueError("steward_region_reference_required")
        if self.region:
            x, y, w, h = self.region
            if not (0 <= x < 1 and 0 <= y < 1 and 0 < w <= 1 and 0 < h <= 1 and x + w <= 1 and y + h <= 1):
                raise ValueError("steward_invalid_region")
        return self


class StewardUnit(Contract):
    unit_id: Digest
    ordinal: int = Field(ge=0)
    title: str = Field(default="", max_length=120)
    text: str = Field(default="", max_length=4000)
    anchor: StewardSourceAnchor
    provider: OpaqueRef
    provider_version: OpaqueRef


class StewardExtractionProvenance(Contract):
    engine: Literal["pdfkit", "vision", "synthetic_fixture"]
    engine_version: str = Field(min_length=1, max_length=120)
    execution: Literal["native", "fixture"]
    method: Literal["embedded_text", "ocr"]
    language_hints: list[str] = Field(default_factory=list, max_length=8)
    raster_content_id: Digest | None = None
    coordinate_origin: Literal["top_left"] = "top_left"

    @model_validator(mode="after")
    def truthful_engine(self):
        if (self.engine == "synthetic_fixture") != (self.execution == "fixture"):
            raise ValueError("steward_invalid_extraction_provenance")
        if self.engine == "pdfkit" and self.method != "embedded_text":
            raise ValueError("steward_pdfkit_cannot_claim_ocr")
        if self.engine == "vision" and self.method != "ocr":
            raise ValueError("steward_vision_method_required")
        return self


class StewardExtractionQuality(Contract):
    assessment: Literal["unassessed", "review_required"] = "unassessed"
    character_count: int = Field(ge=0, le=4000)
    # Raw engine confidence is NOT calibrated OCR accuracy or acceptance evidence.
    ocr_min_confidence: float | None = Field(default=None, ge=0, le=1)


class StewardPageOutcome(Contract):
    """v1 embedded receipts remain valid. v2 expresses genuine OCR/hold/failure evidence."""

    outcome_version: Literal[1, 2] = 1
    anchor: StewardSourceAnchor
    status: Literal["text", "needs_ocr", "empty", "failed", "held"]
    provider: OpaqueRef
    provider_version: OpaqueRef
    unit_id: Digest | None = None
    reason: Literal[
        "embedded_text", "no_embedded_text", "ocr_text", "ocr_no_text", "provider_failure", "provider_hold"
    ]
    provenance: StewardExtractionProvenance | None = None
    quality: StewardExtractionQuality | None = None
    failure_code: (
        Literal[
            "locked",
            "restricted",
            "malformed",
            "missing",
            "limit",
            "unsupported",
            "cancelled",
            "provider_failed",
        ]
        | None
    ) = None

    @model_validator(mode="after")
    def page_evidence(self):
        if self.anchor.kind != "page" or (self.status == "text") != (self.unit_id is not None):
            raise ValueError("steward_invalid_page_outcome")
        if self.outcome_version == 1:
            if self.status not in {"text", "needs_ocr"} or self.reason not in {
                "embedded_text",
                "no_embedded_text",
            }:
                raise ValueError("steward_page_v2_required")
            if (self.status == "text") != (self.reason == "embedded_text"):
                raise ValueError("steward_invalid_page_reason")
            if self.provenance or self.quality or self.failure_code:
                raise ValueError("steward_page_v2_required")
            return self
        if self.provenance is None or self.quality is None:
            raise ValueError("steward_page_provenance_quality_required")
        method = self.provenance.method
        expected = {
            "text": "embedded_text" if method == "embedded_text" else "ocr_text",
            "needs_ocr": "no_embedded_text",
            "empty": "ocr_no_text",
            "failed": "provider_failure",
            "held": "provider_hold",
        }[self.status]
        if (
            self.reason != expected
            or (self.status == "needs_ocr" and method != "embedded_text")
            or (self.status == "empty" and method != "ocr")
        ):
            raise ValueError("steward_invalid_page_reason")
        if (self.status in {"failed", "held"}) != (self.failure_code is not None):
            raise ValueError("steward_page_failure_code_required")
        if (self.status == "text") != (self.quality.character_count > 0):
            raise ValueError("steward_page_character_count_invalid")
        if method == "embedded_text" and self.quality.ocr_min_confidence is not None:
            raise ValueError("steward_unexpected_ocr_confidence")
        return self


class StewardJobRequest(Contract):
    expected_context: Digest
    item_ids: list[Digest] = Field(min_length=1, max_length=32)
    provider: OpaqueRef

    @field_validator("item_ids")
    @classmethod
    def unique_items(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("steward_duplicate_selection")
        return value


class StewardBudget(Contract):
    source_bytes: int = Field(default=1048576, ge=1, le=8388608)
    units: int = Field(default=8, ge=1, le=8)
    seconds: float = Field(default=5, gt=0, le=5)
    text_chars: int = Field(default=32000, ge=1, le=32000)


class StewardJob(Contract):
    job_id: Digest
    item_id: Digest
    content_id: Digest
    source_fingerprint: Digest
    original_ref: FamilyMediaOriginalRef
    provider: OpaqueRef
    provider_version: OpaqueRef
    status: Literal[
        "disabled", "queued", "running", "paused", "done", "needs_ocr", "failed", "stale", "offline"
    ]
    cursor: OpaqueRef | None = None
    completed_units: int = Field(default=0, ge=0, le=256)
    completed_pages: int = Field(default=0, ge=0, le=256)
    source_pages: int | None = Field(default=None, ge=1, le=256)
    charged_source_bytes: int = Field(default=0, ge=0, le=67108864)
    attempts: int = Field(default=0, ge=0, le=64)
    reason: str = Field(default="", max_length=120)
    updated_at: AwareDatetime


class StewardSuggestionRequest(Contract):
    expected_context: Digest
    item_id: Digest


class StewardSuggestion(Contract):
    item_id: Digest
    content_id: Digest
    title: str
    folder: str
    stem: str
    evidence_unit_ids: list[Digest]
    status: Literal["needs_extraction", "display_title_only", "suggested"]
    policy: StewardPolicyResolution


class StewardDestinationInventory(Contract):
    root_ref: OpaqueRef
    namespace_revision: OpaqueRef
    checked_at: AwareDatetime
    complete: bool
    paths: list[str] = Field(default_factory=list, max_length=512)
    basis: Literal["synthetic_fixture", "operator_preview"]

    @field_validator("paths")
    @classmethod
    def validate_paths(cls, value):
        return [relative_path(path) for path in value]


class StewardRelocationSelection(Contract):
    item_id: Digest
    expected_content_id: Digest
    destination: str
    operation: Literal["rename", "move", "rename_and_move", "restructure"] = "rename_and_move"

    @field_validator("destination")
    @classmethod
    def validate_destination(cls, value):
        return relative_path(value)


class StewardPreviewRequest(Contract):
    expected_context: Digest
    selections: list[StewardRelocationSelection] = Field(min_length=1, max_length=32)
    inventory: StewardDestinationInventory


class StewardPlanEntry(Contract):
    entry_id: Digest
    item_id: Digest
    content_id: Digest
    source_fingerprint: Digest
    original_ref: FamilyMediaOriginalRef
    before_relative_path: str | None = None
    destination: str
    operation: Literal["rename", "move", "rename_and_move", "restructure"]
    policy_fingerprint: Digest
    policy_rule_ids: list[OpaqueRef]
    policy_label: str
    blockers: list[str]


class StewardRelocationPlan(Contract):
    plan_id: Digest
    plan_fingerprint: Digest
    context_fingerprint: Digest
    inventory_fingerprint: Digest
    target_root_ref: OpaqueRef
    entries: list[StewardPlanEntry]
    created_at: AwareDatetime
    expires_at: AwareDatetime
    status: Literal["awaiting_review", "blocked", "reviewed_preview", "rejected"]
    counts: dict[str, int]
    execution_enabled: Literal[False] = False


class StewardBatchApproval(Contract):
    expected_context: Digest
    plan_id: Digest
    expected_plan_fingerprint: Digest
    expected_inventory_fingerprint: Digest
    selected_entry_ids: list[Digest] = Field(min_length=1, max_length=32)
    decision: Literal["approve_preview", "reject"]
    # Historical review only. A future executor needs a distinct, scoped grant.
    scope: Literal["preview_only"] = "preview_only"


class StewardJournalEntry(Contract):
    journal_id: Digest
    plan_id: Digest
    entry_id: Digest
    content_id: Digest
    before: FamilyMediaOriginalRef
    before_root_ref: OpaqueRef
    before_relative_path: str | None = None
    target_root_ref: OpaqueRef
    destination: str
    source_fingerprint: Digest
    inventory_fingerprint: Digest
    policy_fingerprint: Digest
    policy_rule_ids: list[OpaqueRef]
    operation: Literal["rename", "move", "rename_and_move", "restructure"]
    phase: Literal["planned", "preview_approved", "rejected", "preflight", "applied", "rollback", "conflict"]
    recorded_at: AwareDatetime
    after_object_ref: OpaqueRef | None = None
    after_revision: OpaqueRef | None = None
    verified_after_hash: Digest | None = None
    inverse_operation: Literal["relocate_no_overwrite"] = "relocate_no_overwrite"
    execution_grant_id: OpaqueRef | None = None

    @model_validator(mode="after")
    def execution_evidence(self):
        if self.phase in {"preflight", "applied", "rollback", "conflict"} and not self.execution_grant_id:
            raise ValueError("steward_execution_grant_required")
        if self.phase in {"preflight", "applied", "rollback", "conflict"} and not self.before_relative_path:
            raise ValueError("steward_durable_before_locator_required")
        if self.phase in {"applied", "rollback"} and not (
            self.after_object_ref and self.after_revision and self.verified_after_hash == self.content_id
        ):
            raise ValueError("steward_journal_hash_verification_required")
        return self


class StewardExecutionBatchGrant(Contract):
    """Future qualification contract only; no route accepts this and execute always refuses."""

    grant_id: OpaqueRef
    reviewed_by: OpaqueRef
    plan_id: Digest
    plan_fingerprint: Digest
    inventory_fingerprint: Digest
    policy_fingerprints: list[Digest] = Field(min_length=1, max_length=32)
    selected_entry_ids: list[Digest] = Field(min_length=1, max_length=32)
    source_grant_id: OpaqueRef
    target_grant_id: OpaqueRef
    approved_at: AwareDatetime
    expires_at: AwareDatetime
    operation: Literal["relocate_no_overwrite"] = "relocate_no_overwrite"
    full_hash_recheck_required: Literal[True] = True
    rollback_journal_required: Literal[True] = True
    overwrite_allowed: Literal[False] = False
    delete_allowed: Literal[False] = False

    @model_validator(mode="after")
    def finite_grant(self):
        if not 0 < (self.expires_at - self.approved_at).total_seconds() <= 300:
            raise ValueError("steward_finite_batch_grant_required")
        if len(set(self.selected_entry_ids)) != len(self.selected_entry_ids):
            raise ValueError("steward_duplicate_selection")
        return self


class StewardCloudContentGrant(Contract):
    """Separate future content permission. Directory policies never grant cloud access."""

    grant_id: OpaqueRef
    provider: OpaqueRef
    model: OpaqueRef
    selected_content_ids: list[Digest] = Field(min_length=1, max_length=32)
    selected_unit_ids: list[Digest] = Field(default_factory=list, max_length=256)
    max_upload_bytes: int = Field(ge=1, le=8388608)
    max_tokens: int = Field(ge=1, le=32000)
    max_cost_minor_units: int = Field(ge=1)
    currency: Literal["CNY", "USD"]
    approved_at: AwareDatetime
    expires_at: AwareDatetime
    automatic_expansion_allowed: Literal[False] = False

    @model_validator(mode="after")
    def finite_content_grant(self):
        if not 0 < (self.expires_at - self.approved_at).total_seconds() <= 300:
            raise ValueError("steward_finite_content_grant_required")
        return self
