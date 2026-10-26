"""Local intent and budgets. Saving these never creates a source/execution grant."""

from typing import Literal

from pydantic import AwareDatetime, Field

from homeplane.contracts import Contract
from homeplane.family_media.contracts import Digest, OpaqueRef

from .contracts import StewardBudget
from .policy import StewardPolicyResolution, StewardPolicyRule


class StewardSettingsRequest(Contract):
    expected_context: Digest
    sample_item_id: Digest


class StewardDirectorySettings(Contract):
    settings_id: Digest
    source_ref: OpaqueRef
    config_generation: int = Field(ge=0)
    connection_epoch: OpaqueRef
    directory_ref: OpaqueRef | None
    watched: bool = False
    extraction_mode: Literal["metadata_only", "pdf_text_first"] = "metadata_only"
    budget: StewardBudget = Field(default_factory=StewardBudget)
    policy_rule_id: OpaqueRef
    revision: int = Field(default=0, ge=0)
    updated_at: AwareDatetime


class StewardSettingsView(Contract):
    settings: StewardDirectorySettings
    rule: StewardPolicyRule
    effective_policy: StewardPolicyResolution
    fingerprint: Digest
    saved: bool
    activation: Literal["held"] = "held"
    source_grant_verified: Literal[False] = False
    scan_started: Literal[False] = False
    extraction_started: Literal[False] = False


class StewardSettingsEdit(StewardSettingsRequest):
    expected_fingerprint: Digest
    watched: bool
    extraction_mode: Literal["metadata_only", "pdf_text_first"]
    budget: StewardBudget
    rule: StewardPolicyRule
