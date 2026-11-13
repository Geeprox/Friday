"""通过原本机接口处理已准入的语音文字。"""

import json
import sys
import time

from .arl_intent import IntentRuntime
from .intent import LAYA_QUESTIONS, PLAN_INSTRUCTIONS, address, classify_input, parse_plan
from .paths import PRIVATE
from .voice_preferences import read_preferences


def _classify(text, route):
    try:
        from homeplane.shared_laya import SharedLaya

        return SharedLaya().predict(classify_input(text, route), LAYA_QUESTIONS)["answers"]["command"]
    except Exception:
        return {"status": "unavailable"}


def run(request):
    began = time.monotonic()
    policy = read_preferences()
    if (request.get("op") != "delegate" or request.get("voice_revision") != policy.revision
            or (PRIVATE / "voice-input-paused").exists()):
        raise ValueError("voice_preferences_changed")
    text, context = request.get("text"), request.get("context")
    if not isinstance(context, dict):
        raise ValueError("invalid_task")
    route = address(text, context.get("attention") is True, wake_word=policy.wake_word,
                    after_wake=context.get('after_wake') is True)
    if not route["admitted"]:
        raise ValueError("invalid_task")

    laya = _classify(text, route)
    if read_preferences() != policy or (PRIVATE / "voice-input-paused").exists():
        raise ValueError("voice_preferences_changed")
    if laya.get("choice") == "闲聊或转述" and laya.get("confidence", 0) >= .75:
        return dict(type="ignored", laya=laya)

    prompt = PLAN_INSTRUCTIONS + "\n输入数据：" + json.dumps(
        {"text": text, "address": dict(route, person_id=context.get("person_id")), "laya": laya},
        ensure_ascii=False,
    )
    intent = IntentRuntime()
    try:
        result = intent.complete(prompt, request_id=request.get("request_id"), voice_revision=policy.revision)
        if result.get("simulated") is not False:
            raise RuntimeError("non_live_provider")
        plan = parse_plan(str(result.get("text", "")))
        return dict(type="task_result", provider=result.get("provider"), model=result.get("model"),
                    simulated=False, plan=plan, text=plan["reply"], laya=laya,
                    execution_scope="codex_plan_local_allowlisted_actuator", runtime=intent.report(),
                    latency_ms=round((time.monotonic() - began) * 1000, 1))
    finally:
        intent.close()


def main():
    def emit(result):
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")), flush=True)

    emit({"type": "ready"})
    for line in sys.stdin.buffer:
        request = {}
        try:
            if len(line) > 16000:
                raise ValueError("input_too_large")
            request = json.loads(line)
            if not isinstance(request, dict):
                request = {}
                raise ValueError("invalid_task")
            emit(dict(run(request), id=request["id"]))
        except Exception as error:
            emit(dict(type="error", id=request.get("id"), error_class=type(error).__name__))


if __name__ == "__main__":
    main()
