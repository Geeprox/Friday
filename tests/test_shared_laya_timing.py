"""Synthetic ARL deadlines and privacy-safe latency metadata; never run a model."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip('arl', reason='Optional shared ARL SDK')
from arl.contracts import Fault
from homeplane import shared_laya_service as shared
from homeplane.runtime_log import RuntimeLog

QUESTIONS = {'command': {'type': 'choice', 'instructions': 'Choose',
                         'criteria': {'yes': 'request', 'no': 'chat'}}}


@pytest.fixture
def synthetic(tmp_path, monkeypatch):
    clock = SimpleNamespace(wall=10000., mono=100.)
    monkeypatch.setattr(shared, 'time', SimpleNamespace(time=lambda: clock.wall))
    agent = Mock()
    agent.predict.return_value = {'answers': {'command': {'choice': 'yes', 'confidence': .9}},
                                  'usage': {'truncated': False}}
    engine = shared.LayaEngine(tmp_path, {}, factory=lambda: agent,
                               clock=lambda: clock.mono, log=RuntimeLog(tmp_path / 'logs'))
    monkeypatch.setattr(engine, 'watchdog', lambda: SimpleNamespace(cancel=lambda: None))
    engine.warm()
    request = {'state': 'synthetic-private-input', 'questions': QUESTIONS,
               'deadline_at_ms': int(clock.wall * 1000) + 4000}
    yield engine, agent, clock, request
    assert engine.close_logs()


def test_wall_clock_rollback_cannot_extend_an_admitted_deadline(synthetic):
    engine, agent, clock, request = synthetic
    reply = agent.predict.return_value
    def slow(*_):
        clock.mono += 5
        clock.wall -= 30
        return reply
    agent.predict.side_effect = slow
    with pytest.raises(Fault, match='deadline_exceeded'):
        engine.predict(request)
    assert engine.calls == 0 and engine.failures == 1
    assert engine.lock.acquire(blocking=False)
    engine.lock.release()


def test_deadline_expiring_during_admission_skips_model_work(synthetic):
    engine, agent, clock, request = synthetic
    def delayed_watchdog():
        clock.mono += 4.1
        return SimpleNamespace(cancel=lambda: None)
    engine.watchdog = delayed_watchdog
    with pytest.raises(Fault, match='deadline_exceeded'):
        engine.predict(request)
    agent.predict.assert_not_called()
    assert engine.calls == 0


def test_serialization_is_inside_the_request_deadline(synthetic, monkeypatch):
    engine, _, clock, request = synthetic
    original = shared.json.dumps
    def serialize(value, **kwargs):
        raw = original(value, **kwargs)
        if isinstance(value, dict) and 'answers' in value:
            clock.mono += 4.1
        return raw
    monkeypatch.setattr(shared, 'json', SimpleNamespace(dumps=serialize))
    with pytest.raises(Fault, match='deadline_exceeded'):
        engine.predict(request)
    assert engine.calls == 0


def test_wall_clock_forward_jump_still_fails_closed(synthetic):
    engine, agent, clock, request = synthetic
    reply = agent.predict.return_value
    def changed_clock(*_):
        clock.mono += .1
        clock.wall += 60
        return reply
    agent.predict.side_effect = changed_clock
    with pytest.raises(Fault, match='deadline_exceeded'):
        engine.predict(request)


def test_status_reports_bounded_idle_model_and_total_time_without_content(synthetic):
    engine, agent, clock, request = synthetic
    assert engine.status()['last_call'] is None
    clock.mono += 600
    clock.wall += 600
    request['deadline_at_ms'] = int(clock.wall * 1000) + 4000
    reply = agent.predict.return_value
    def short(*_):
        clock.mono += .25
        clock.wall += .25
        return reply
    agent.predict.side_effect = short
    engine.predict(request)
    timing = engine.status()['last_call']
    assert timing == dict(idle_ms=600000, model_ms=250, serialization_ms=0,
                          total_ms=250, result='ok', code='none')
    timing['model_ms'] = -1
    assert engine.status()['last_call']['model_ms'] == 250
    snapshot = str(engine.status()) + str(engine.log.export())
    assert request['state'] not in snapshot and QUESTIONS['command']['instructions'] not in snapshot
    assert 'deadline_at_ms' not in snapshot
    assert engine.loads == 1


def test_failed_model_call_records_only_a_fixed_error_code(synthetic):
    engine, agent, clock, request = synthetic
    def failed(*_):
        clock.mono += .125
        raise RuntimeError('synthetic-private-error')
    agent.predict.side_effect = failed
    with pytest.raises(Fault, match='runtime_error'):
        engine.predict(request)
    timing = engine.status()['last_call']
    assert timing['result'] == 'error' and timing['code'] == 'runtime_error'
    assert timing['model_ms'] == 125 and timing['total_ms'] == 125
    assert 'synthetic-private-error' not in str(engine.status()) + str(engine.log.export())


def test_skipped_call_reports_no_model_or_serialization_work(synthetic):
    engine, agent, _, request = synthetic
    engine.agent = None
    with pytest.raises(Fault, match='not_ready'):
        engine.predict(request)
    assert engine.status()['last_call'] == dict(idle_ms=None, model_ms=None, serialization_ms=None,
                                              total_ms=0, result='unavailable', code='not_ready')
    agent.predict.assert_not_called()


def test_idle_durations_are_bounded_and_not_reset_by_a_skipped_request(synthetic):
    engine, agent, clock, request = synthetic
    resident = engine.agent
    clock.mono += 2 * 86400
    clock.wall += 2 * 86400
    request['deadline_at_ms'] = int(clock.wall * 1000) + 4000
    engine.agent = None
    with pytest.raises(Fault, match='not_ready'):
        engine.predict(request)
    engine.agent = resident
    engine.predict(request)
    assert engine.status()['last_call']['idle_ms'] == 86400000
    assert engine.loads == 1 and agent.predict.call_count == 1


def test_unrecognized_provider_fault_cannot_enter_status_metadata(synthetic):
    engine, agent, _, request = synthetic
    agent.predict.side_effect = Fault('synthetic-private-provider-code')
    with pytest.raises(Fault):
        engine.predict(request)
    assert engine.status()['last_call']['code'] == 'unclassified'
    assert 'synthetic-private-provider-code' not in str(engine.status()) + str(engine.log.export())


def test_timed_out_call_does_not_break_the_next_fresh_request(synthetic):
    engine, agent, clock, request = synthetic
    reply = agent.predict.return_value
    def slow(*_):
        clock.mono += 5
        clock.wall += 5
        return reply
    agent.predict.side_effect = slow
    with pytest.raises(Fault, match='deadline_exceeded'):
        engine.predict(request)
    request['deadline_at_ms'] = int(clock.wall * 1000) + 4000
    agent.predict.side_effect = None
    assert engine.predict(request)['loads'] == 1
    assert engine.calls == 1 and engine.failures == 1
    assert engine.status()['last_call']['code'] == 'none'


def test_malformed_reply_is_not_counted_as_a_successful_call(synthetic):
    engine, agent, _, request = synthetic
    agent.predict.return_value = {'usage': {}}
    with pytest.raises(Fault, match='runtime_error'):
        engine.predict(request)
    assert engine.calls == 0 and engine.failures == 1


def test_response_assembly_cannot_return_after_the_deadline(synthetic):
    engine, agent, clock, request = synthetic
    class SlowLookup(dict):
        def __getitem__(self, key):
            if key == 'answers':
                clock.mono += 4.1
            return super().__getitem__(key)
    agent.predict.return_value = SlowLookup(agent.predict.return_value)
    with pytest.raises(Fault, match='deadline_exceeded'):
        engine.predict(request)
    assert engine.calls == 0 and engine.failures == 1
