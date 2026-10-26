"""Provider declarations and a seam, not bundled parsers/models/services."""

from dataclasses import dataclass, field
from typing import Protocol

from .contracts import StewardBudget, StewardJob, StewardPageOutcome, StewardUnit


@dataclass(frozen=True)
class ProviderSpec:
    key: str
    version: str
    label: str
    environment: str
    qualification: str


PROVIDERS = {
    spec.key: spec
    for spec in (
        ProviderSpec(
            "pdfkit_text", "system_pdfkit_v1", "PDF 文字", "local", "native_slot_scope_memory_pages"
        ),
        ProviderSpec("docling", "unqualified", "文档提取", "local", "format_page_memory_sandbox"),
        ProviderSpec("ocr", "unqualified", "截图文字", "local", "zh_layout_region_accuracy"),
        ProviderSpec("asr_keyframes", "unqualified", "视频与音频", "local", "timestamps_all_scene_coverage"),
        ProviderSpec(
            "cloud_semantic", "unqualified", "内容归纳", "cloud", "selected_content_provider_budget_grant"
        ),
    )
}


@dataclass(frozen=True)
class ExtractionStep:
    units: list[StewardUnit]
    next_cursor: str | None
    done: bool
    page_outcomes: list[StewardPageOutcome] = field(default_factory=list)
    source_pages: int | None = None


class ExtractionProvider(Protocol):
    """Later: isolated bounded reader capability; never paths/credentials/global NAS access."""

    spec: ProviderSpec

    async def step(self, job: StewardJob, budget: StewardBudget) -> ExtractionStep: ...
