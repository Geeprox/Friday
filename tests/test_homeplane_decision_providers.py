import asyncio
import json
import time
from dataclasses import asdict

import httpx
import pytest

from homeplane.decision import CalibrationProfile, DecisionRequest, DecisionResult
from scripts.spikes.direct_jev import (
    JEV_ENDPOINT,
    ConservativeDecisionGate,
    JevDecisionProvider,
    ProviderTaskCalibration,
    TrustedTokenAccounting,
    registry_descriptors,
)

MODEL = "simulated-jev-1.0.0"  # Explicit fixture identifier, not an upstream model claim.
SECRET = "fixture-only-not-a-real-credential"


def request(**kw):
    return DecisionRequest("addressed_to_home", {"text": "simulated home input", "simulated": True},
                           kw.pop("deadline", time.monotonic() + 2), **kw)


def reply(**kw):
    body = {"model": MODEL, "answers": {"addressed_to_home": {"type": "choice", "choice": "yes",
             "probabilities": {"yes": .98, "no": .01, "unknown": .01}}},
            "usage": {"input_tokens": 50}}
    body.update(kw)
    return body


def calibrated(**kw):
    return {"addressed_to_home": ProviderTaskCalibration("jev", kw.get("model", MODEL), "1",
        CalibrationProfile("addressed_to_home", "simulated-holdout-v1", holdout_validated=True))}


def provider(handler=None, **kw):
    config = dict(model=MODEL, cloud_opt_in=True, api_key=SECRET,
                  accounting=TrustedTokenAccounting(MODEL, "synthetic-counter-v1", lambda payload: 50),
                  transport=httpx.MockTransport(handler or (lambda req: httpx.Response(200, json=reply()))))
    config.update(kw)
    return JevDecisionProvider(**config)


@pytest.mark.asyncio
async def test_exact_wire_contract_and_no_actions():
    seen = []
    def handle(req):
        seen.append(req)
        assert str(req.url) == JEV_ENDPOINT and req.method == "POST"
        assert req.headers['authorization'] == 'Bearer ' + SECRET
        body = json.loads(req.content)
        assert body['model'] == MODEL
        assert isinstance(body['state'], str) and json.loads(body['state'])['simulated']
        assert body['questions']['addressed_to_home']['criteria'] == {'yes':'yes','no':'no','unknown':'unknown'}
        return httpx.Response(200, json=reply())
    result = await provider(handle, profiles=calibrated()).decide(request())
    assert len(seen) == 1 and result.label == 'yes' and not result.abstained
    assert result.shadow and not result.actionable and result.token_count == 50
    assert SECRET not in json.dumps(asdict(result))


@pytest.mark.asyncio
@pytest.mark.parametrize('config,reason', [
    ({'cloud_opt_in':False}, 'cloud_opt_in_required'),
    ({'api_key':None}, 'credential_required'),
    ({'accounting':None}, 'budget_unverified'),
    ({'accounting':TrustedTokenAccounting('wrong-1','v1',lambda p:50)}, 'budget_unverified'),
    ({'accounting':TrustedTokenAccounting(MODEL,'v1',lambda p:1025)}, 'context_overflow'),
    ({'accounting':TrustedTokenAccounting(MODEL,'v1',lambda p:True)}, 'budget_unverified'),
    ({'profiles':calibrated(model='wrong-1')}, 'calibration_scope_mismatch'),
])
async def test_preflight_rejects_without_upload(config, reason):
    calls = []
    def handle(req):
        calls.append(req)
        raise AssertionError('Must never send')
    result = await provider(handle, **config).decide(request())
    assert result.unknown_reason == reason and not calls
    assert result.metrics['upload_attempted'] is False


@pytest.mark.asyncio
async def test_byte_budget_and_task_version_fail_before_count_or_send():
    calls = []
    def count(_):
        calls.append(True)
        return 50
    engine = provider(accounting=TrustedTokenAccounting(MODEL,'synthetic-counter-v1',count))
    result = await engine.decide(DecisionRequest('intent',{'text':'x'*65537},time.monotonic()+2))
    assert result.unknown_reason == 'context_overflow' and not calls
    result = await engine.decide(request(task_version='2'))
    assert result.unknown_reason == 'unsupported_task_version' and not calls


@pytest.mark.asyncio
async def test_uncalibrated_high_score_is_unknown():
    result = await provider().decide(request())
    assert result.scores['yes'] == .98
    assert result.label == 'unknown' and result.unknown_reason == 'uncalibrated_task'
    assert not result.actionable


@pytest.mark.asyncio
@pytest.mark.parametrize('scores', [
    {'yes':1}, {'yes':.9,'no':.9,'unknown':.1}, {'yes':float('nan'),'no':0,'unknown':0},
    {'yes':True,'no':0,'unknown':0}, {'yes':1.1,'no':-.1,'unknown':0},
])
async def test_malformed_scores_abstain(scores):
    body = reply()
    body['answers']['addressed_to_home']['probabilities'] = scores
    result = await provider(lambda _:httpx.Response(200, content=json.dumps(body)), profiles=calibrated()).decide(request())
    assert result.unknown_reason == 'invalid_scores' and result.abstained and not result.actionable


@pytest.mark.asyncio
@pytest.mark.parametrize('body,reason', [
    (reply(model='simulated-other-2'), 'provider_model_version_mismatch'),
    (reply(usage={'input_tokens':1025}), 'response_token_budget_invalid'),
    (reply(usage={'input_tokens':49}), 'token_accounting_mismatch'),
    ({'secret':SECRET}, 'provider_protocol_or_transport_error'),
])
async def test_version_and_response_budget(body,reason):
    result = await provider(lambda _:httpx.Response(200,json=body)).decide(request())
    assert result.unknown_reason == reason and SECRET not in json.dumps(asdict(result))


@pytest.mark.asyncio
async def test_timeout_busy_cancellation_and_no_backlog():
    started, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def handle(req):
        calls.append(req)
        started.set()
        await release.wait()
        return httpx.Response(200,json=reply())
    engine = provider(handle,timeout=.02)
    first = asyncio.create_task(engine.decide(request()))
    await started.wait()
    second = await engine.decide(request())
    assert second.unknown_reason == 'provider_busy'
    result = await first
    assert result.unknown_reason == 'provider_timeout' and len(calls) == 1
    started.clear()
    task = asyncio.create_task(engine.decide(request()))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not engine._busy.locked()
    release.set()
    assert (await engine.decide(request())).unknown_reason == 'uncalibrated_task'


@pytest.mark.asyncio
async def test_deadline_discards_and_http_does_not_leak():
    result = await provider().decide(request(deadline=time.monotonic()-1))
    assert result.unknown_reason == 'expired' and not result.metrics['upload_attempted']
    engine = provider(lambda _:httpx.Response(401,text=SECRET))
    result = await engine.decide(request())
    assert result.unknown_reason == 'provider_http_error' and SECRET not in str(asdict(result))
    result = await provider(lambda _:httpx.Response(200,text='x'*65537)).decide(request())
    assert result.unknown_reason == 'response_overflow'


def test_registry_no_invented_openai_or_auto_model():
    descriptors = registry_descriptors()
    item = next(d for d in descriptors if d['provider_id']=='openai_decision_api')
    assert item['availability']=='unavailable' and 'endpoint' not in item
    assert all(not d['automatic_fallback'] and not d['action_authority'] for d in descriptors)
    for model in ['jev-latest','auto','jev','']:
        with pytest.raises(ValueError, match='explicit_versioned_model_required'):
            JevDecisionProvider(model=model)


def test_gate_reasserts_shadow_unknown_and_task_specificity():
    gate = ConservativeDecisionGate('jev',MODEL,calibrated())
    unsafe = DecisionResult('addressed_to_home',label='yes',abstained=False,actionable=True,shadow=False)
    result = gate.apply(request(), {'yes':0,'no':0,'unknown':1},unsafe)
    assert result.label=='unknown' and result.abstained and result.shadow and not result.actionable
    wrong = ConservativeDecisionGate('laya',MODEL,calibrated())
    result = wrong.apply(request(), {'yes':1,'no':0,'unknown':0},unsafe)
    assert result.unknown_reason=='calibration_scope_mismatch' and not result.actionable
