import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from scripts.spikes.direct_laya import (
    CalibrationProfile,
    DecisionRequest,
    LayaMLXDecisionProvider,
    baseline,
    inspect_input,
)


def checkpoint(tmp_path):
    for name in ["model.safetensors", "rl_agent_config.json", "encoder/config.json",
                 "tokenizer/tokenizer.json", "tokenizer/tokenizer_config.json"]:
        f = tmp_path / name
        f.parent.mkdir(exist_ok=True)
        f.write_text("{}")
    return tmp_path


class Worker:
    returncode = 0
    def __init__(self, scores=None, delay=0):
        self.scores = scores or {"yes": .98, "no": .01, "unknown": .01}
        self.delay = delay
        self.killed = False
        self.stdin = self
        self.stdout = self
        self.read_done = False
    def write(self, data):
        pass
    async def drain(self):
        pass
    def close(self):
        pass
    async def read(self, count):
        await asyncio.sleep(self.delay)
        if self.read_done:
            return b""
        self.read_done = True
        return json.dumps({"scores": self.scores, "token_count": 51}).encode()
    async def communicate(self, data):
        await asyncio.sleep(self.delay)
        return json.dumps({"scores": self.scores, "token_count": 51}).encode(), b""
    def kill(self):
        self.killed = True
        self.returncode = -9
    async def wait(self):
        return self.returncode


@pytest.mark.asyncio
async def test_missing_and_expired_never_spawn(tmp_path):
    engine = LayaMLXDecisionProvider(tmp_path)
    assert (await engine.decide(DecisionRequest("intent", {}, time.monotonic() - 1))).unknown_reason == "expired"
    assert (await engine.decide(DecisionRequest("intent", {}, time.monotonic() + 1))).unknown_reason == "local_model_missing"


@pytest.mark.asyncio
async def test_raw_score_is_not_accuracy_and_uncalibrated_abstains(tmp_path):
    engine = LayaMLXDecisionProvider(checkpoint(tmp_path))
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=Worker())) as spawn:
        result = await engine.decide(DecisionRequest("addressed_to_home", {}, time.monotonic() + 10))
    assert result.unknown_reason == "uncalibrated_task" and result.label == "unknown"
    assert result.scores["yes"] == .98 and not result.actionable and result.shadow
    assert "HOME" not in spawn.call_args.kwargs["env"]
    assert spawn.call_args.kwargs["env"]["HF_HUB_OFFLINE"] == "1"


@pytest.mark.asyncio
async def test_calibration_task_scope_and_fixed_shadow(tmp_path):
    profile = CalibrationProfile("addressed_to_home", "simulated-v1", holdout_validated=True)
    engine = LayaMLXDecisionProvider(checkpoint(tmp_path), profiles={"addressed_to_home": profile})
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=Worker())):
        result = await engine.decide(DecisionRequest("addressed_to_home", {}, time.monotonic() + 10))
    assert result.label == "yes" and result.shadow and not result.actionable
    engine.profiles["addressed_to_home"] = CalibrationProfile("intent")
    assert (await engine.decide(DecisionRequest("addressed_to_home", {}, time.monotonic() + 10))).unknown_reason == "calibration_task_mismatch"


@pytest.mark.asyncio
async def test_timeout_kills_child_and_does_not_replay(tmp_path):
    engine = LayaMLXDecisionProvider(checkpoint(tmp_path), timeout=.01)
    worker = Worker(delay=.1)
    worker.returncode = None
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=worker)):
        result = await engine.decide(DecisionRequest("intent", {}, time.monotonic() + 1))
    assert result.unknown_reason == "worker_timeout" and worker.killed and result.abstained


@pytest.mark.asyncio
async def test_cancellation_kills_worker(tmp_path):
    engine = LayaMLXDecisionProvider(checkpoint(tmp_path))
    worker = Worker(delay=1)
    worker.returncode = None
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=worker)):
        task = asyncio.create_task(engine.decide(DecisionRequest("intent", {}, time.monotonic() + 10)))
        await asyncio.sleep(.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert worker.killed


@pytest.mark.asyncio
async def test_invalid_or_missing_unknown_scores_rejected(tmp_path):
    engine = LayaMLXDecisionProvider(checkpoint(tmp_path))
    for scores in ({"yes": 1}, {"yes": float("nan"), "no": 0, "unknown": 0}):
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=Worker(scores))):
            result = await engine.decide(DecisionRequest("addressed_to_home", {}, time.monotonic() + 1))
        assert result.unknown_reason == "worker_protocol_error"


def test_floor_cannot_be_lowered():
    with pytest.raises(ValueError):
        CalibrationProfile("intent", minimum_score=.1)
    with pytest.raises(ValueError):
        CalibrationProfile("intent", temperature=.5)
    assert baseline("intent", {"text": "不要打开客厅灯"}) == "unknown"


def test_real_checkpoint_tokenizer_budget_if_available():
    model = Path("data/models/decision")
    if not (model / "tokenizer/tokenizer.json").exists():
        pytest.skip("Local model tokenizer optional; no download")
    pytest.importorskip("tokenizers")
    count, _ = inspect_input(model, DecisionRequest("intent", {"text": "客厅灯打开", "simulated": True}, 100))
    assert count == 53
    with pytest.raises(ValueError, match="context_overflow"):
        inspect_input(model, DecisionRequest("intent", {"text": "hello " * 1500}, 100))


@pytest.mark.asyncio
async def test_real_subprocess_output_bound():
    import sys

    from scripts.spikes.direct_laya import exchange_bounded
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import sys;sys.stdout.write('x'*1000000)",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
    try:
        with pytest.raises(ValueError, match="worker_output_overflow"):
            await exchange_bounded(proc, b"", limit=128)
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.communicate()


def test_model_manifest_rejects_changed_files(tmp_path):
    from scripts.spikes.decision_worker import verify_model
    manifest = json.loads(Path("homeplane/laya-model-manifest.json").read_text())
    for name in manifest["files"]:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"changed")
    with pytest.raises(ValueError, match="model_integrity_mismatch"):
        verify_model(tmp_path)
