"""Local PCM -> Whisper -> addressing + Laya; complex tasks use ARL Codex.

Input/output are bounded private pipes. No audio/transcript files or model downloads.
"""

import base64
import contextlib
import json
import os
import signal
import sys
import time
import warnings

import numpy as np

from homeplane.embodied.household import basic_command
from homeplane.embodied.intent import (
    LAYA_QUESTIONS,
    PLAN_INSTRUCTIONS,
    address,
    classify_input,
    parse_plan,
    transcript_quality,
)
from homeplane.embodied.local_asr import LocalASR
from homeplane.embodied.local_intent import household_plan

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
warnings.filterwarnings("ignore")


def emit(x):
    print(json.dumps(x, ensure_ascii=False, separators=(",", ":")), flush=True)


def normalize_audio(samples):
    rms = float(np.sqrt(np.mean(samples * samples)))
    gain = min(24.0, 0.08 / max(rms, 1e-7))
    return np.clip(samples * gain, -0.98, 0.98), rms, gain


def main():
    from homeplane.shared_laya import SharedLaya

    asr = LocalASR(os.environ.get("FRIDAY_ASR", "sensevoice"))
    agent = SharedLaya()
    intent_runtime = None
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    # Load/warm only the existing local checkpoint. Silence never becomes a command.
    with contextlib.redirect_stdout(sys.stderr):
        asr.transcribe(np.zeros(16000, dtype=np.float32))
    try:
        laya_status = agent.status()
    except Exception:
        laya_status = {"state": "unavailable"}
    emit({"type": "ready", "asr": asr.label, "semantic": "shared_laya_command_gate", "audio_persisted": False, "intelligence": {"laya": "arl_shared", "laya_state": laya_status.get("state"), "laya_via_shared_arl": True, "codex": "on_demand_scoped_text", "agent_tools": False}})
    for line in sys.stdin:
        request = {}
        try:
            if len(line) > 400000:
                raise ValueError("input_too_large")
            request = json.loads(line)
            began = time.monotonic()
            op = request.get("op")
            if op == "transcribe":
                pcm = base64.b64decode(request["pcm"], validate=True)
                if not 6400 <= len(pcm) <= 256000:
                    raise ValueError("invalid_audio_duration")
                samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
                samples, rms, gain = normalize_audio(samples)
                with contextlib.redirect_stdout(sys.stderr):
                    text, quality = asr.transcribe(samples)
                valid, quality_reason = transcript_quality(text, quality)
                route = (
                    address(text, request.get("attending") is True)
                    if valid
                    else {"admitted": False, "reason": quality_reason}
                )
                semantic = None
                if route["admitted"]:
                    if household_plan(text, request.get("attending") is True) or basic_command(
                        text, request.get("attending") is True
                    ):
                        semantic = {
                            "choice": "指令或任务请求",
                            "confidence": 1.0,
                            "method": "strict_local_grammar",
                        }
                    else:
                        try:
                            semantic = agent.predict(classify_input(text, route), LAYA_QUESTIONS)["answers"][
                                "command"
                            ]
                        except Exception:
                            semantic = {"status": "unavailable"}
                emit(
                    {
                        "id": request["id"],
                        "type": "transcript",
                        "text": text if valid else "",
                        "address": route,
                        "laya": semantic,
                        "asr_quality": dict(quality, filter=quality_reason),
                        "signal": {"input_rms": round(rms, 4), "gain": round(gain, 2)},
                        "latency_ms": round((time.monotonic() - began) * 1000, 1),
                    }
                )
            elif op == "delegate":
                text = request.get("text", "")
                if not isinstance(text, str) or not 1 <= len(text) <= 1000:
                    raise ValueError("invalid_task")
                if intent_runtime is None:
                    import asyncio

                    from homeplane.arl import ARLRuntime

                    status = asyncio.run(ARLRuntime().status(timeout=4))
                    provider = next(
                        (
                            p
                            for p in status.get("providers", [])
                            if p["name"] == "codex_app_server" and p["availability"] == "ready"
                        ),
                        None,
                    )
                    if provider is None:
                        raise RuntimeError("arl_codex_not_ready")
                    from homeplane.embodied.arl_intent import IntentRuntime

                    intent_runtime = IntentRuntime(provider["models"][0])
                prompt = (
                    PLAN_INSTRUCTIONS
                    + "\n输入数据："
                    + json.dumps(
                        {
                            "text": text,
                            "address": request.get("context", {}),
                            "laya": request.get("laya", {}),
                        },
                        ensure_ascii=False,
                    )
                )
                result = intent_runtime.complete(prompt)
                if result.get("simulated") is not False:
                    raise RuntimeError("non_live_provider")
                plan = parse_plan(str(result.get("text", "")))
                emit(
                    {
                        "id": request["id"],
                        "type": "task_result",
                        "provider": result.get("provider"),
                        "model": result.get("model"),
                        "simulated": False,
                        "plan": plan,
                        "text": plan["reply"],
                        "execution_scope": "codex_plan_local_allowlisted_actuator",
                        "runtime": intent_runtime.report(),
                        "latency_ms": round((time.monotonic() - began) * 1000, 1),
                    }
                )
            else:
                raise ValueError("operation_not_allowed")
        except Exception as e:
            emit(
                {
                    "id": request.get("id"),
                    "type": "error",
                    "error_class": type(e).__name__,
                    "code": getattr(e, "code", None),
                    "runtime": intent_runtime.report() if intent_runtime else None,
                }
            )


if __name__ == "__main__":
    main()
