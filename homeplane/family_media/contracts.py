"""Family originals and review-only recipes; these are Friday consumer contracts."""

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from homeplane.contracts import Contract

Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
OpaqueRef = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.:-]{1,160}$")]
MediaStatus = Literal["ready", "pending", "missing", "stale", "unavailable"]


class FamilyMediaOriginalRef(Contract):
    source_ref: OpaqueRef
    object_ref: OpaqueRef
    config_generation: int = Field(ge=0)
    connection_epoch: OpaqueRef
    revision: OpaqueRef
    revision_basis: Literal["metadata"] = "metadata"


class FamilyMediaItem(Contract):
    item_id: Digest
    asset_id: Digest | None = None
    media_kind: Literal["image", "video", "audio", "other"]
    size_bytes: int = Field(ge=0)
    status: MediaStatus
    reason: str
    original_hash: Digest | None = None
    previous_original_hashes: list[Digest] = Field(default_factory=list, max_length=8)
    hash_basis: Literal["full_stream_sha256", "not_verified"]
    hash_progress_bytes: int = Field(ge=0)
    completion_evidence: Literal["source_closed", "operator_reviewed_stable_metadata", "unreviewed"]
    last_seen_at: AwareDatetime
    captured_at: Literal[None] = None
    original_ref: FamilyMediaOriginalRef | None = None
    source_revision_basis: Literal["metadata"] = "metadata"
    immutable_source_verified: Literal[False] = False
    playback_verified: Literal[False] = False
    simulated: bool
    media_copied: Literal[False] = False


class FamilyMediaCheckpoint(Contract):
    binding_fingerprint: Digest
    directory_ref: OpaqueRef | None = None
    scan_id: OpaqueRef
    next_cursor: OpaqueRef | None = None
    entries_seen: int = Field(ge=0)
    enumeration_complete: bool
    completed_scan_token: OpaqueRef | None = None
    snapshot_consistent: Literal[False] = False
    checked_at: AwareDatetime


class FamilyMediaContext(Contract):
    fingerprint: Digest
    source_ref: OpaqueRef | None
    config_generation: int = Field(ge=0)
    connection_epoch: OpaqueRef | None
    source_online: bool
    failure: str | None = None
    completion_convention: Literal["unreviewed", "closed_files_operator_reviewed"]
    checkpoint: FamilyMediaCheckpoint | None = None
    simulated: bool
    read_only_originals: Literal[True] = True
    media_copied: Literal[False] = False
    device_write_attempted: Literal[False] = False
    model_inference_attempted: Literal[False] = False


class FamilyMediaScanRequest(Contract):
    expected_context: Digest
    directory_ref: OpaqueRef | None = None
    limit: int = Field(default=128, ge=1, le=128)
    byte_budget: int = Field(default=4194304, ge=0, le=8388608)
    restart_scan: bool = False


class FamilyMediaScanResult(Contract):
    context: FamilyMediaContext
    files_observed: int = Field(ge=0, le=128)
    bytes_read: int = Field(ge=0, le=8388608)
    bytes_requested: int = Field(ge=0, le=8388608)
    hashes_completed: int = Field(ge=0, le=4)
    checkpoint: FamilyMediaCheckpoint
    simulated: bool
    media_copied: Literal[False] = False


class FamilyMediaSourceReview(Contract):
    expected_context: Digest
    completion_convention: Literal["unreviewed", "closed_files_operator_reviewed"]


class FamilyMediaQuery(Contract):
    expected_context: Digest
    cursor: Digest | None = None
    limit: int = Field(default=50, ge=1, le=100)
    media_kind: Literal["image", "video", "audio", "other"] | None = None
    asset_id: Digest | None = None


class FamilyMediaPage(Contract):
    context_fingerprint: Digest
    items: list[FamilyMediaItem] = Field(max_length=100)
    next_cursor: Digest | None = None
    simulated: bool


class FamilyMediaAssetRequest(Contract):
    expected_context: Digest
    asset_id: Digest


class FamilyMediaAssetEvidence(Contract):
    context_fingerprint: Digest
    asset_id: Digest
    original_hash: Digest
    status: MediaStatus
    originals: list[FamilyMediaItem] = Field(max_length=128)
    original_count: int = Field(ge=1)
    ready_original_count: int = Field(ge=0)
    other_original_count: int = Field(ge=0)
    partial: bool
    dedup_basis: Literal["full_stream_sha256"] = "full_stream_sha256"
    near_duplicate_inference: Literal[False] = False
    causal_relation: Literal["unknown"] = "unknown"
    simulated: bool


class FamilyMediaResolveRequest(Contract):
    expected_context: Digest
    item_id: Digest


class FamilyMediaResolution(Contract):
    context_fingerprint: Digest
    item: FamilyMediaItem
    source_checked: bool
    simulated: bool
    media_copied: Literal[False] = False


class FamilyMediaOriginalPlan(Contract):
    plan_id: Digest
    context_fingerprint: Digest
    item_id: Digest
    original_hash: Digest
    media_kind: Literal["image", "video", "audio", "other"]
    size_bytes: int = Field(gt=0)
    stream_url: str = Field(pattern=r"^/api/family-media/originals/[a-f0-9]{64}$")
    expires_at: AwareDatetime
    range_budget_bytes: Literal[1048576] = 1048576
    session_budget_bytes: Literal[67108864] = 67108864
    playback_verified: Literal[False] = False
    media_copied: Literal[False] = False
    simulated: bool


class FamilyMediaOrganize(Contract):
    operation: Literal["organize"] = "organize"
    album: str = Field(min_length=1, max_length=120)
    labels: list[str] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def bounded_labels(self):
        if any(not label.strip() or len(label) > 80 for label in self.labels):
            raise ValueError("family_media_invalid_label")
        if not self.album.strip():
            raise ValueError("family_media_invalid_album")
        return self


class FamilyMediaCrop(Contract):
    operation: Literal["crop"] = "crop"
    x: float = Field(ge=0, lt=1)
    y: float = Field(ge=0, lt=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)
    coordinate_basis: Literal["original_normalized_after_exif_orientation"] = (
        "original_normalized_after_exif_orientation"
    )

    @model_validator(mode="after")
    def inside_original(self):
        if self.x + self.width > 1 or self.y + self.height > 1:
            raise ValueError("family_media_crop_outside_original")
        return self


class FamilyMediaRotate(Contract):
    operation: Literal["rotate"] = "rotate"
    degrees_clockwise: Literal[0, 90, 180, 270]


class FamilyMediaExposure(Contract):
    operation: Literal["exposure"] = "exposure"
    stops: float = Field(ge=-2, le=2)


FamilyMediaOperation = Annotated[
    FamilyMediaOrganize | FamilyMediaCrop | FamilyMediaRotate | FamilyMediaExposure,
    Field(discriminator="operation"),
]


class FamilyMediaRecipeRequest(FamilyMediaResolveRequest):
    expected_original_hash: Digest
    operations: list[FamilyMediaOperation] = Field(min_length=1, max_length=16)


class FamilyMediaRecipe(Contract):
    recipe_id: Digest
    context_fingerprint: Digest
    item_id: Digest
    original_hash: Digest
    original_ref: FamilyMediaOriginalRef
    operations: list[FamilyMediaOperation] = Field(min_length=1, max_length=16)
    status: Literal["awaiting_review", "reviewed", "rejected"]
    created_at: AwareDatetime
    expires_at: AwareDatetime
    reviewed_at: AwareDatetime | None = None
    simulated: bool
    original_mutated: Literal[False] = False
    automatic_delete: Literal[False] = False
    rendered_derivative: Literal[False] = False


class FamilyMediaRecipeReview(Contract):
    expected_context: Digest
    recipe_id: Digest
    decision: Literal["reviewed", "rejected"]
