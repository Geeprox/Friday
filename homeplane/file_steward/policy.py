"""Pinned directory policy and file exceptions; filenames/classifiers confer no authority."""

from typing import Literal

from pydantic import Field

from homeplane.contracts import Contract
from homeplane.family_media.catalog import digest
from homeplane.family_media.contracts import Digest, OpaqueRef


class StewardPolicyRule(Contract):
    rule_id: OpaqueRef
    source_ref: OpaqueRef
    config_generation: int = Field(ge=0)
    connection_epoch: OpaqueRef
    directory_ref: OpaqueRef | None = None
    item_id: Digest | None = None
    label: str = Field(min_length=1, max_length=80)
    status: Literal["proposed", "user_pinned"] = "proposed"
    origin: Literal["unknown", "camera_phone", "downloaded", "old_photos", "sensitive", "bundle"] = "unknown"
    rename_preview: bool = False
    move_preview: bool = False
    structure_preview: bool = False
    dedup_review: bool = False
    unit: Literal["file", "dependent_bundle"] = "file"


class StewardPolicyResolution(Contract):
    fingerprint: Digest
    source: Literal["file_exception", "directory_policy", "default_preserve", "policy_conflict"]
    matched_rule_ids: list[OpaqueRef]
    label: str
    rename_preview: bool = False
    move_preview: bool = False
    structure_preview: bool = False
    dedup_review: bool = False
    bundle_review_required: bool = False
    automatic_cloud_allowed: Literal[False] = False


# Candidates for per-folder discussion only; never installed or inferred from names.
PRESETS = [
    {
        "key": "camera_phone",
        "label": "手机与相机原片",
        "rename_preview": False,
        "move_preview": False,
        "structure_preview": False,
        "dedup_review": True,
    },
    {
        "key": "downloaded",
        "label": "下载的网页图片",
        "applies_to": "operator_confirmed_web_images",
        "rename_preview": True,
        "move_preview": True,
        "structure_preview": True,
        "dedup_review": True,
    },
    {
        "key": "old_photos",
        "label": "旧照片 · 来源待核对",
        "rename_preview": False,
        "move_preview": False,
        "structure_preview": False,
        "dedup_review": True,
    },
    {
        "key": "sensitive",
        "label": "证件与保险",
        "rename_preview": False,
        "move_preview": False,
        "structure_preview": False,
        "dedup_review": False,
    },
    {
        "key": "bundle",
        "label": "手册、EPUB 与课程资料包",
        "rename_preview": False,
        "move_preview": False,
        "structure_preview": False,
        "dedup_review": True,
        "unit": "dependent_bundle",
    },
]


def resolve_policy(rules, *, source_ref, config_generation, connection_epoch, item_id, ancestor_refs):
    """ancestor_refs must come from the namespace owner, ordered root→leaf, never AI."""
    candidates = []
    for rule in rules:
        if rule.status != "user_pinned" or (
            rule.source_ref,
            rule.config_generation,
            rule.connection_epoch,
        ) != (source_ref, config_generation, connection_epoch):
            continue
        if rule.item_id == item_id:
            candidates.append((10000, rule))
        elif rule.item_id is None and rule.directory_ref in ancestor_refs:
            candidates.append((ancestor_refs.index(rule.directory_ref), rule))
    selected = (
        [rule for rank, rule in candidates if rank == max(r for r, _ in candidates)] if candidates else []
    )
    signatures = {
        tuple(
            getattr(rule, key)
            for key in ("rename_preview", "move_preview", "structure_preview", "dedup_review", "unit")
        )
        for rule in selected
    }
    conflict = len(signatures) > 1
    source = (
        "policy_conflict"
        if conflict
        else "file_exception"
        if selected and selected[0].item_id
        else "directory_policy"
        if selected
        else "default_preserve"
    )
    values = {
        key: bool(selected) and not conflict and all(getattr(rule, key) for rule in selected)
        for key in ("rename_preview", "move_preview", "structure_preview", "dedup_review")
    }
    # A parent bundle boundary cannot be broken by a lone member exception.
    bundle = any(rule.unit == "dependent_bundle" for _, rule in candidates)
    ids = sorted(rule.rule_id for rule in selected)
    label = (
        "目录规则冲突 · 保留原样"
        if conflict
        else "；".join(rule.label for rule in selected)
        if selected
        else "未设置目录策略 · 保留原样"
    )
    return StewardPolicyResolution(
        fingerprint=digest([source, [r.model_dump(mode="json") for r in selected], values, bundle]),
        source=source,
        matched_rule_ids=ids,
        label=label,
        bundle_review_required=bundle,
        **values,
    )


class StewardPolicyEdit(Contract):
    expected_context: Digest
    rule: StewardPolicyRule
