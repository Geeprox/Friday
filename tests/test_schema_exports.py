import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 基线已存在的公开文件差异。同步前核对兼容版本和实际消费方。
EXISTING_DIFFERENCES = {
    "ActionTrace.json",
    "ArchiveCameraEdit.json",
    "ArchivePlaybackPlan.json",
    "CoreRulePlan.json",
    "CoreRuleProposal.json",
    "CoreRuleRecord.json",
    "DevicePlanRequest.json",
    "GeekBackgroundRequest.json",
    "HomeDeviceAuthority.json",
    "LightExecutionTrace.json",
    "RuleAnalysisResult.json",
    "RuleTextResult.json",
    "RuleTrial.json",
    "Snapshot.json",
}


def test_public_schema_exports_match_the_tracked_contracts(tmp_path):
    outputs = {}
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(ROOT), environment.get("PYTHONPATH")) if part
    )
    for name, arguments in (
        ("console", ("homeplane.cli", "schema")),
        ("family-media", ("homeplane.family_media.schemas",)),
        ("file-steward", ("homeplane.file_steward.schemas",)),
    ):
        destination = tmp_path / name
        destination.mkdir()
        subprocess.run([sys.executable, "-B", "-m", *arguments], cwd=destination, env=environment, check=True, timeout=30)
        for schema in (destination / "schemas").glob("*.json"):
            assert schema.name not in outputs, schema.name
            outputs[schema.name] = json.loads(schema.read_bytes())

    differences = {
        name for name, value in outputs.items()
        if value != json.loads((ROOT / "schemas" / name).read_bytes())
    }
    assert differences == EXISTING_DIFFERENCES
