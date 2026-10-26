import json
import time
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
pytest.importorskip('arl', reason='Optional shared ARL SDK')
from arl.contracts import Fault
from homeplane.shared_laya import SharedLaya, validate_input, VERSION
from homeplane.shared_laya_service import LayaEngine, SharedRuntime

Q = {"command": {"type": "choice", "instructions": "Choose", "criteria": {"yes": "request", "no": "chat"}}}


class Agent:
    def predict(self, state, questions):
        return {"answers": {"command": {"choice": "yes", "confidence": .9}}, "usage": {"truncated": False}}


def params():
    return {"state": "private synthetic text", "questions": Q, "deadline_at_ms": int(time.time()*1000)+4000}


def test_one_model_reused_and_no_input_logs(tmp_path):
    from homeplane.runtime_log import RuntimeLog
    log = RuntimeLog(tmp_path)
    engine = LayaEngine(tmp_path, {}, factory=Agent, log=log)
    engine.warm()
    engine.warm()
    for _ in range(3):
        assert engine.predict(params())["loads"] == 1
    assert engine.status()["calls"] == 3
    assert engine.status()["resident_models"] == 1
    assert engine.flush_logs()
    text = (tmp_path/"runtime.jsonl").read_text()
    assert "synthetic" not in text and "request" not in text
    rows = [json.loads(x) for x in text.splitlines()]
    assert rows[-1]["correlation"] == rows[-2]["correlation"]


def test_concurrent_call_rejected_without_second_agent(tmp_path):
    began, release = threading.Event(), threading.Event()
    class Slow(Agent):
        def predict(self, *args):
            began.set(); release.wait(2)
            return super().predict(*args)
    engine = LayaEngine(tmp_path, {}, factory=Slow)
    engine.warm()
    with ThreadPoolExecutor() as pool:
        first = pool.submit(engine.predict, params())
        assert began.wait(1)
        try:
            with pytest.raises(Fault, match="runtime_busy"):
                engine.predict(params())
        finally:
            release.set()
        assert first.result()["loads"] == 1


def test_deadline_and_truncation_are_not_actionable(tmp_path):
    class Truncated(Agent):
        def predict(self, *args):
            result = super().predict(*args); result["usage"]["truncated"] = True
            return result
    engine = LayaEngine(tmp_path, {}, factory=Truncated); engine.warm()
    with pytest.raises(Fault, match="request_budget_exceeded"):
        engine.predict(params())
    with pytest.raises(Fault, match="deadline_exceeded"):
        engine.predict(dict(params(), deadline_at_ms=1))


@pytest.mark.parametrize("choice", [[], {}, None, 1, "unknown"])
def test_client_rejects_invalid_answers(choice):
    class Client:
        def rpc(self, *_):
            return {"runtime":"arl_shared_laya", "version":VERSION, "simulated":False,
                    "answers":{"command":{"choice":choice,"confidence":.5}}}
    with pytest.raises(Fault, match="invalid_response"):
        SharedLaya(client=Client()).predict("x", Q)


def test_no_unverified_rpc(tmp_path):
    engine = LayaEngine(tmp_path, {}, factory=Agent)
    runtime = SharedRuntime(tmp_path, engine, {"friday": {"executable": "/unknown", "sha256": "x"}})
    request = {"protocol":"arl/1", "app":"friday", "method":"laya.predict.v1", "params":params()}
    with pytest.raises(Fault, match="app_identity_unverified"):
        runtime.dispatch(request)
    assert runtime.execute_jobs is False and runtime.policies["friday"].max_calls == 0


@pytest.mark.parametrize("state,questions", [("x"*8001,Q),("x",{}),("x",{"x":{"type":"code"}})])
def test_input_bounds(state, questions):
    with pytest.raises(Fault):
        validate_input(state, questions)


def test_residency_budget_is_bounded_and_restores_previous_limit(tmp_path):
    class Memory:
        wired = 1234
        cache = None
        cleared = False
        def device_info(self):
            return dict(memory_size=512*1024**2,max_recommended_working_set_size=256*1024**2)
        def set_wired_limit(self,value):
            before=self.wired;self.wired=value;return before
        def set_cache_limit(self,value):self.cache=value
        def clear_cache(self):self.cleared=True
    (tmp_path/'model.safetensors').write_bytes(b'synthetic')
    mx=Memory();engine=LayaEngine(tmp_path,{})
    engine.reserve_memory(mx)
    assert mx.wired==32*1024**2
    assert engine.status()['wired_budget_bytes']==mx.wired
    assert mx.cache<=32*1024**2
    engine.release_memory()
    assert mx.wired==1234 and mx.cleared
    assert engine.status()['wired_budget_bytes']==0
