"""家庭自动化的本地配置、审核计划和持久执行回执。"""

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool, model_validator

from .contracts import Contract
from .rule_contracts import RuleCompilation, RuleEvaluation, RuleIR, digest


class CatalogSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    key: str = Field(min_length=1, max_length=160)
    entry_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class CatalogOrigin(CatalogSelection):
    source: Literal["geek", "mijia"]
    kind: Literal["automation", "scene"]
    name: str = Field(min_length=1, max_length=160)


class CoreRuleRecord(Contract):
    id: str
    rule: RuleIR
    enabled: bool
    source_scope: str
    authority_revision: str
    fingerprint: str
    catalog_origin: CatalogOrigin | None = None
    source: Literal["project:home_core"] = "project:home_core"
    simulated: Literal[False] = False

    def with_enabled(self, enabled):
        return core_record(self.id, self.rule, enabled, self.source_scope, self.authority_revision,
                           catalog_origin=self.catalog_origin)


class CoreRuleProposal(Contract):
    rule: RuleIR
    expected_context: str = Field(pattern=r"^[a-f0-9]{64}$")
    idempotency_key: str = Field(min_length=1, max_length=128)
    expires_at: AwareDatetime
    record_id: str | None = Field(default=None, min_length=1, max_length=128)
    expected_fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    catalog_selection: CatalogSelection | None = None
    start_paused: StrictBool = False

    @model_validator(mode="after")
    def owned_scope(self):
        if self.rule.simulated or self.rule.execution.preferred_backend != "home_core":
            raise ValueError("explicit_real_home_core_scope_required")
        if bool(self.record_id) != bool(self.expected_fingerprint):
            raise ValueError("complete_source_choice_required")
        if self.record_id and self.catalog_selection:
            raise ValueError("catalog_rebuild_requires_new_rule")
        return self


class CoreRulePlan(Contract):
    id: str
    boot_id: str
    context_fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    created_at: AwareDatetime
    expires_at: AwareDatetime
    old: CoreRuleRecord | None
    new: CoreRuleRecord
    compilation: RuleCompilation
    approval_digest: str
    status: Literal["validated", "awaiting_approval", "verified", "rolled_back", "failed", "expired"]
    start_paused: bool = False
    code: str
    trial: RuleEvaluation | None = None
    real_write_attempted: Literal[False] = False
    physically_confirmed: Literal[False] = False
    simulated: Literal[False] = False


class CoreRuleApproval(Contract):
    plan_id: str = Field(min_length=1, max_length=128)
    approval_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class CoreRuleRun(Contract):
    id: str
    record_id: str
    rule_id: str
    fingerprint: str
    event_id: str
    boot_id: str
    created_at: AwareDatetime
    expires_at: AwareDatetime
    status: Literal["running", "waiting", "completed", "partial", "cancelled", "suppressed", "unknown_outcome"]
    code: str
    effects: list[dict] = Field(default_factory=list, max_length=16)
    simulated: Literal[False] = False
    real_write_attempted: bool = False
    physically_confirmed: Literal[False] = False


def core_record(identifier, rule, enabled, scope, authority, *, catalog_origin=None):
    body = dict(
        id=identifier,
        rule=rule.model_dump(mode="json"),
        enabled=enabled,
        source_scope=scope,
        authority_revision=authority,
    )
    if catalog_origin is not None:
        body["catalog_origin"] = catalog_origin.model_dump(mode="json")
    return CoreRuleRecord(**body, fingerprint=digest(body))
