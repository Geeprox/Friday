"""Client for the single account-wide ARL Laya service. Never loads a model.

The service uses ARL's existing transport/identity/lifetime machinery. Inference
is ephemeral: unlike durable ARL jobs, speech-derived input is never stored.
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

from arl.client import Client
from arl.contracts import Fault

HOME = Path.home() / "Library/Application Support/ARL/laya-v1"
PREDICT = "laya.predict.v1"
STATUS = "laya.status.v1"
VERSION = "0.5.0"


def validate_input(state, questions):
    # A bounded typed decision, never model paths, hooks, code or tool execution.
    if type(state) not in (dict, str, list) or type(questions) is not dict or not 1 <= len(questions) <= 8:
        raise Fault("invalid_arguments")
    try:
        raw = json.dumps({"state": state, "questions": questions}, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        raise Fault("invalid_arguments") from None
    if len(raw.encode()) > 8000:
        raise Fault("request_budget_exceeded")
    for key, question in questions.items():
        if (type(key) is not str or not 1 <= len(key) <= 64 or type(question) is not dict
                or set(question) - {"type", "instructions", "criteria"}
                or question.get("type") != "choice"
                or type(question.get("instructions")) is not str
                or not 1 <= len(question["instructions"]) <= 1000
                or type(question.get("criteria")) is not dict
                or not 2 <= len(question["criteria"]) <= 10
                or any(type(k) is not str or type(v) is not str or not 1 <= len(k) <= 100
                       or not 1 <= len(v) <= 500 for k, v in question["criteria"].items())):
            raise Fault("invalid_arguments")


class SharedLaya:
    def __init__(self, home=HOME, *, app="friday", client=None, clock=time.monotonic):
        self.client = client or Client(Path(home), app)
        self.clock = clock

    def status(self):
        return self.client.rpc(STATUS)

    def predict(self, state, questions):
        validate_input(state, questions)
        expires = self.clock() + 4
        result = self.client.rpc(PREDICT, {"state": state, "questions": questions,
                                         "deadline_at_ms": int(time.time() * 1000) + 4000})
        # The transport/framework can finish after the host's final check.
        # Never admit that late reply, even after a wall-clock correction.
        if self.clock() >= expires:
            raise Fault("deadline_exceeded")
        if (type(result) is not dict or result.get("runtime") != "arl_shared_laya"
                or result.get("version") != VERSION or result.get("simulated") is not False
                or type(result.get("answers")) is not dict or set(result["answers"]) != set(questions)):
            raise Fault("invalid_response")
        for key, answer in result["answers"].items():
            confidence = answer.get("confidence") if type(answer) is dict else None
            if (type(answer) is not dict or type(answer.get("choice")) is not str
                    or answer["choice"] not in questions[key]["criteria"]
                    or type(confidence) not in (int, float) or not math.isfinite(confidence)
                    or not 0 <= confidence <= 1):
                raise Fault("invalid_response")
        return result
