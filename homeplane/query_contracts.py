"""Scoped home reads; transport-owned grants are deliberately not request fields."""

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from .contracts import Contract
from .family_media.contracts import FamilyMediaOriginalRef

Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")]
Label = Annotated[str, Field(max_length=256, pattern=r"^[^\x00-\x1f\x7f]*$")]
Selector = Annotated[Label, Field(min_length=1, max_length=128)]


class HomeQuery(Contract):
    kind: Literal["state", "devices", "history", "evidence"]
    source: Literal["home", "family_media"] = "home"
    source_ref: Identifier | None = None
    expected_context: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")] | None = None
    rooms: list[Selector] = Field(default_factory=list, max_length=8, json_schema_extra={"uniqueItems": True})
    entities: list[Identifier] = Field(default_factory=list, max_length=16, json_schema_extra={"uniqueItems": True})
    capabilities: list[Identifier] = Field(default_factory=list, max_length=8, json_schema_extra={"uniqueItems": True})
    evidence_refs: list[Identifier] = Field(default_factory=list, max_length=16, json_schema_extra={"uniqueItems": True})
    start: AwareDatetime | None = None
    end: AwareDatetime | None = None
    limit: int = Field(default=20, ge=1, le=50, strict=True)
    cursor: str | None = Field(default=None, min_length=1, max_length=1024)
    timeout_ms: int = Field(default=3000, ge=10, le=3000, strict=True)

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_version(cls, value):
        if type(value) is not int:
            raise ValueError("invalid_query_version")
        return value

    @field_validator("start", "end", mode="before")
    @classmethod
    def explicit_time(cls, value):
        if value is not None and not isinstance(value, (str, datetime)):
            raise ValueError("explicit_history_time_required")
        return value

    @model_validator(mode="after")
    def bounded_selection(self):
        family = self.source == "family_media"
        if family:
            if (self.kind != "evidence" or not self.source_ref or not self.expected_context
                    or self.rooms or self.entities or self.capabilities or self.start or self.end):
                raise ValueError("explicit_family_media_selection_required")
            if not self.evidence_refs or any(not re.fullmatch(r"family-media:[a-f0-9]{64}", ref)
                                             for ref in self.evidence_refs):
                raise ValueError("explicit_family_media_asset_refs_required")
        elif self.source_ref is not None or self.expected_context is not None:
            raise ValueError("family_media_context_only")
        if not family and not self.rooms and not self.entities:
            raise ValueError("explicit_query_selection_required")
        for values in (self.rooms, self.entities, self.capabilities, self.evidence_refs):
            if len(set(values)) != len(values) or any(
                not 0 < len(v) <= 128 or any(ord(c) < 32 for c in v) for v in values
            ):
                raise ValueError("invalid_query_selection")
        if not family and self.kind in {"history", "evidence"}:
            if (
                self.start is None
                or self.end is None
                or not 0 < (self.end - self.start).total_seconds() <= 86400
            ):
                raise ValueError("bounded_history_window_required")
        elif self.start is not None or self.end is not None:
            raise ValueError("history_window_only")
        if self.kind != "state" and self.capabilities:
            raise ValueError("state_capabilities_only")
        if (self.kind == "evidence") != bool(self.evidence_refs):
            raise ValueError("explicit_evidence_refs_required")
        if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", ref) for ref in self.evidence_refs):
            raise ValueError("opaque_evidence_refs_required")
        if len(self.model_dump_json(exclude={"cursor"}).encode()) > 4096:
            raise ValueError("query_input_budget")
        return self


@dataclass(frozen=True)
class ReadScope:
    """Trusted composition supplies this after identity/permission checks, never JSON.

    loopback_console is the existing local Console boundary, not OS app identity.
    An ARL host must supply registered_peer plus an actual approved scoped grant.
    """

    subject: str
    revision: str
    expires_at: datetime
    boundary: Literal["loopback_console", "registered_peer", "unverified"] = "unverified"
    rooms: frozenset[str] = frozenset()
    entities: frozenset[str] = frozenset()
    all_home: bool = False
    history: bool = False
    permitted: bool = False
    family_media_sources: frozenset[str] = frozenset()

    def __post_init__(self):
        if (
            self.boundary not in {"loopback_console", "registered_peer", "unverified"}
            or any(type(value) is not bool for value in (self.all_home, self.history, self.permitted))
            or type(self.subject) is not str or not 0 < len(self.subject) <= 64
            or type(self.revision) is not str or not 0 < len(self.revision) <= 128
            or type(self.rooms) is not frozenset or type(self.entities) is not frozenset
            or type(self.family_media_sources) is not frozenset or len(self.family_media_sources) > 16
            or not isinstance(self.expires_at, datetime)
            or self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None
            or len(self.rooms) > 64
            or len(self.entities) > 256
            or any(type(key) is not str or not 0 < len(key) <= 128
                   for key in (*self.rooms, *self.entities, *self.family_media_sources))
            or any(ord(c) < 32 or ord(c) == 127
                   for text in (self.subject, self.revision, *self.rooms, *self.entities, *self.family_media_sources)
                   for c in text)
        ):
            raise ValueError("invalid_read_scope")


class QueryItem(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    @model_validator(mode="after")
    def metadata_budget(self):
        if len(self.model_dump_json().encode()) > 8192:
            raise ValueError("query_item_budget")
        for name in ("excluded_sources", "summary"):
            value = getattr(self, name, {})
            if len(value) > 8 or any(len(key) > 128 for key in value):
                raise ValueError("query_metadata_budget")
        if hasattr(self, "summary") and set(self.summary) - {"from", "to", "quality", "status", "code", "reason"}:
            raise ValueError("invalid_query_summary")
        return self


Scalar = bool | Annotated[int, Field(ge=-1e12, le=1e12)] | Annotated[float, Field(ge=-1e12, le=1e12)] | Label | None


class HomeQuerySourceTime(QueryItem):
    physical_source_id: Identifier
    ingest_source: Identifier
    measured_at: AwareDatetime | None
    source_reported_at: AwareDatetime | None
    integration_written_at: AwareDatetime | None
    received_at: AwareDatetime
    freshness_basis: Literal["device_sample", "upstream_report", "cache_write", "unknown"]
    cached: bool | None
    clock_uncertainty_seconds: float | None = Field(default=None, ge=0)


class HomeQueryState(QueryItem):
    kind: Literal["state"]
    id: Annotated[str, Field(max_length=256, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")]
    entity_id: Identifier
    device_id: Identifier | None
    device_name: Label | None
    name: Label
    room: Annotated[Label, Field(max_length=128)] | None
    capability: Identifier
    value: Scalar
    unit: str | None = Field(max_length=64)
    quality: Literal["healthy", "suspect", "stale", "faulty", "unknown"]
    freshness: Literal["canonical_valid", "expired", "unknown"]
    reasons: list[Label] = Field(max_length=17)
    selected_source: Identifier | None
    excluded_sources: dict[Identifier, Label] = Field(max_length=8)
    as_of: AwareDatetime
    valid_until: AwareDatetime | None
    as_of_basis: Literal["canonical_resolution_not_hardware_sample"]
    value_interval: tuple[float, float] | None
    diagnostic_interval: tuple[float, float] | None
    calibration_version: str | None = Field(max_length=128)
    resolver_version: str = Field(max_length=128)
    inferred: bool
    simulated: bool
    confidence: None
    evidence_refs: list[Identifier] = Field(max_length=32)
    source_times: list[HomeQuerySourceTime] = Field(max_length=8)


class HomeQueryDevice(QueryItem):
    kind: Literal["device"]
    id: Identifier
    name: Label
    room: Annotated[Label, Field(max_length=128)] | None
    category: Literal["device", "service"]
    identity_basis: Literal["xiaomi_identifier", "ha_device_registry", "simulated"]
    function_ids: list[Identifier] = Field(max_length=256)
    simulated: bool
    execution_authority: Literal[False]


class HomeQueryEvent(QueryItem):
    kind: Literal["event"]
    id: Identifier
    source: Identifier
    event_kind: Literal["state-snapshot", "state-transition", "source-report", "quality-change", "action-result", "observation-gap"]
    entity_refs: list[Identifier] = Field(max_length=256)
    event_time: AwareDatetime
    source_reported_at: AwareDatetime | None
    event_time_basis: Literal["upstream_event", "local_event", "unknown"]
    observed_at: AwareDatetime | None
    query_observed_at: AwareDatetime
    hardware_sample_time: None
    causation_ref: Identifier | None
    causation_status: Literal["recorded_by_source_not_independently_verified", "unknown"]
    correlation_ref: Identifier | None
    summary: dict[Identifier, Scalar] = Field(
        max_length=6,
        json_schema_extra={"propertyNames": {"enum": ["from", "to", "quality", "status", "code", "reason"]}},
    )
    simulated: bool
    confidence: None


class HomeQueryPermissionScope(QueryItem):
    subject: Label
    boundary: Literal["loopback_console", "registered_peer", "unverified"]
    revision: Label
    requested_rooms: list[Label] = Field(max_length=8)
    requested_entities: list[Label] = Field(max_length=16)
    requested_source: Literal["home", "family_media"] = "home"
    requested_source_ref: Identifier | None = None


Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class HomeQueryFamilyMediaOriginal(QueryItem):
    item_id: Digest
    media_kind: Literal["image", "video"]
    status: Literal["ready", "pending", "missing", "stale", "unavailable"]
    last_seen_at: AwareDatetime
    original_ref: FamilyMediaOriginalRef | None


class HomeQueryFamilyMediaEvidence(QueryItem):
    kind: Literal["family_media_evidence"]
    id: Identifier
    asset_id: Digest
    original_hash: Digest
    source_ref: Identifier
    context_fingerprint: Digest
    config_generation: int = Field(ge=0)
    connection_epoch: Identifier | None
    status: Literal["ready", "pending", "missing", "stale", "unavailable"]
    original_count: int = Field(ge=1, le=20000)
    ready_original_count: int = Field(ge=0, le=20000)
    other_original_count: int = Field(ge=0, le=20000)
    original_count_basis: Literal["observed_source_bindings_including_historical_epochs"]
    originals: list[HomeQueryFamilyMediaOriginal] = Field(max_length=8)
    originals_truncated: bool
    query_observed_at: AwareDatetime
    captured_at: None = None
    hardware_sample_time: None = None
    physical_copy_count: None = None
    causal_relation: Literal["unknown"] = "unknown"
    confidence: None = None
    hash_basis: Literal["full_stream_sha256"] = "full_stream_sha256"
    source_revision_basis: Literal["metadata"] = "metadata"
    immutable_source_verified: Literal[False] = False
    playback_verified: Literal[False] = False
    simulated: bool

    @model_validator(mode="after")
    def evidence_invariants(self):
        if (self.asset_id != self.original_hash or self.id != "family-media:" + self.asset_id
                or self.ready_original_count + self.other_original_count != self.original_count
                or len(self.originals) > self.original_count
                or (not self.originals_truncated and len(self.originals) != self.original_count)
                or any(original.original_ref and original.original_ref.source_ref != self.source_ref
                       for original in self.originals)):
            raise ValueError("invalid_family_media_evidence")
        return self


ITEM_MODELS = {"state": HomeQueryState, "device": HomeQueryDevice, "event": HomeQueryEvent,
               "family_media_evidence": HomeQueryFamilyMediaEvidence}


class HomeQueryResult(Contract):
    status: Literal["ok", "partial", "empty", "refused"]
    code: Identifier
    kind: Literal["state", "devices", "history", "evidence"]
    source: Literal["home", "family_media"] = "home"
    observed_at: AwareDatetime
    snapshot_at: AwareDatetime | None = None
    snapshot_ref: Identifier | None = None
    items: list[dict] = Field(default_factory=list, max_length=50)
    next_cursor: str | None = Field(default=None, max_length=1024)
    coverage: Identifier
    limitations: list[Identifier] = Field(default_factory=list, max_length=12)
    permission_scope: dict
    simulated: bool | None = None
    confidence: None = None
    confidence_basis: Literal["not_statistically_calibrated"] = "not_statistically_calibrated"
    execution_authority: Literal[False] = False
    household_media_sent: Literal[False] = False
    sensitivity: Literal["household"] = "household"

    @field_validator("items")
    @classmethod
    def validate_items(cls, values):
        # Keep the existing Console's dict interface, with a closed wire schema.
        result = []
        for value in values:
            model = ITEM_MODELS.get(value.get("kind"))
            if model is None:
                raise ValueError("invalid_query_item")
            result.append(model.model_validate(value).model_dump(mode="json"))
        return result

    @field_validator("permission_scope")
    @classmethod
    def validate_scope(cls, value):
        return HomeQueryPermissionScope.model_validate(value).model_dump(mode="json")

    @model_validator(mode="after")
    def result_invariants(self):
        expected = "family_media_evidence" if self.source == "family_media" else (
            "state" if self.kind == "state" else "device" if self.kind == "devices" else "event"
        )
        if self.source == "family_media" and self.kind != "evidence":
            raise ValueError("query_result_source_mismatch")
        if any(item["kind"] != expected for item in self.items):
            raise ValueError("query_result_kind_mismatch")
        if self.status == "refused" and (self.items or self.next_cursor):
            raise ValueError("refused_query_has_content")
        return self

    @classmethod
    def model_json_schema(cls, *args, **kwargs):
        schema = super().model_json_schema(*args, **kwargs)
        items = TypeAdapter(HomeQueryState | HomeQueryDevice | HomeQueryEvent | HomeQueryFamilyMediaEvidence).json_schema()
        schema.setdefault("$defs", {}).update(items.pop("$defs", {}))
        schema["properties"]["items"]["items"] = items
        schema["properties"]["permission_scope"] = HomeQueryPermissionScope.model_json_schema()
        return schema


def arl_query_descriptor():
    """Proposal uses the verified arl/1 schema subset; it is not registration.

    Arrays/unions in HomeQuery cannot pass upstream 0.1.1 validate. The complete
    bounded typed request is encoded as JSON and revalidated inside Friday.
    Permission labels alone do not grant access; host identity+scope are required.
    """
    return {
        "name": "friday.home.query",
        "version": 1,
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["request_json"],
            "properties": {"request_json": {"type": "string", "minLength": 1, "maxLength": 8192}},
        },
        "idempotent": True,
        "permissions": ["friday_household_read"],
        "availability": "unsupported_transport",
        "description": "Scoped state/device/history/evidence metadata read; no actions, media or credentials",
        "registered": False,
        "identity_verified": False,
        "transport": "pending_upstream_authenticated_app_capability_bridge",
        "data_sensitivity": "household",
        "result_retention": "pending_host_policy",
        "cloud_egress": "not_authorized",
    }


def decode_arl_query(arguments):
    try:
        oversized = type(arguments) is dict and isinstance(arguments.get("request_json"), str) and (
            len(arguments["request_json"].encode()) > 8192
        )
    except UnicodeError:
        raise ValueError("invalid_query_wrapper") from None
    if (
        type(arguments) is not dict
        or set(arguments) != {"request_json"}
        or not isinstance(arguments["request_json"], str)
        or oversized
    ):
        raise ValueError("invalid_query_wrapper")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_query_field")
            result[key] = value
        return result

    try:
        return HomeQuery.model_validate(json.loads(arguments["request_json"], object_pairs_hook=unique))
    except (ValueError, TypeError, RecursionError):
        raise ValueError("invalid_query_json") from None
