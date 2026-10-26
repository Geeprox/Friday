"""Bounded logging and receiving deadlines, without models or real I/O."""
import threading
import time
from types import SimpleNamespace

import pytest
pytest.importorskip('arl', reason='Optional shared ARL SDK')
from arl.contracts import Fault
from homeplane.runtime_log import RuntimeLog
from homeplane.shared_laya import SharedLaya, VERSION
from homeplane.shared_laya_service import DeferredLog, LayaEngine

Q={'x':{'type':'choice','instructions':'synthetic','criteria':{'a':'one','b':'two'}}}


def test_blocked_result_writer_does_not_block_prediction_or_status(tmp_path):
    entered, release = threading.Event(), threading.Event()
    class Sink(RuntimeLog):
        def record(self, stage, event, **fields):
            if stage=='arl.complete' and event=='result':
                entered.set()
                assert release.wait(2)
            return super().record(stage,event,**fields)
    class Agent:
        def predict(self,*_):return {'answers':{'x':{'choice':'a','confidence':.9}},'usage':{}}
    engine=LayaEngine(tmp_path,{},factory=Agent,log=Sink(tmp_path/'logs'))
    engine.warm()
    try:
        began=time.monotonic()
        reply=engine.predict(dict(state='private synthetic input',questions=Q,deadline_at_ms=int(time.time()*1000)+4000))
        assert time.monotonic()-began<1 and reply['loads']==1
        assert entered.wait(1)
        assert engine.status()['last_call']['result']=='ok'
        assert engine.status()['log_pending']>=1
        assert not engine.close_logs(timeout=.01)
    finally:
        release.set()
        assert engine.close_logs()
    assert 'private synthetic input' not in (tmp_path/'logs/runtime.jsonl').read_text()


def test_backpressure_is_bounded_and_never_adds_a_second_writer():
    entered,release=threading.Event(),threading.Event()
    threads=set()
    class Sink:
        def record(self,*_,**__):
            threads.add(threading.get_ident());entered.set();assert release.wait(2)
    log=DeferredLog(Sink(),capacity=2)
    try:
        log.record('arl.worker','start');assert entered.wait(1)
        for _ in range(20):log.record('arl.worker','result')
        assert log.queue.qsize()==2 and log.dropped==18 and len(threads)==1
        assert not log.close(.01)
        log.record('arl.worker','result')
        assert log.dropped==19
    finally:
        release.set();assert log.close()


def test_sink_failure_is_counted_without_propagating_exception_text():
    class Sink:
        def record(self,*_,**__):raise RuntimeError('private-error-must-not-escape')
    log=DeferredLog(Sink());log.record('arl.worker','start')
    assert log.close() and log.failures==1 and log.dropped==0


def test_idle_writer_exits_and_later_metadata_can_start_a_new_writer():
    rows=[]
    class Sink:
        def record(self,*args,**kwargs):rows.append(args)
    log=DeferredLog(Sink());log.record('arl.worker','start');assert log.flush()
    thread=log.thread;thread.join(1)
    assert not thread.is_alive()
    log.record('arl.worker','result');assert log.close()
    assert rows==[('arl.worker','start'),('arl.worker','result')]


def test_receiver_discards_a_reply_delayed_in_the_transport():
    clock=[100.]
    class Client:
        def rpc(self,*_):
            clock[0]+=4.1
            return dict(runtime='arl_shared_laya',version=VERSION,simulated=False,
                        answers={'x':{'choice':'a','confidence':.9}})
    with pytest.raises(Fault,match='deadline_exceeded'):
        SharedLaya(client=Client(),clock=lambda:clock[0]).predict('synthetic',Q)


def test_writer_allocation_failure_cannot_poison_inference_lock(tmp_path,monkeypatch):
    class Agent:
        def predict(self,*_):return {'answers':{'x':{'choice':'a','confidence':.9}},'usage':{}}
    engine=LayaEngine(tmp_path,{},factory=Agent)
    monkeypatch.setattr(engine,'watchdog',lambda:SimpleNamespace(cancel=lambda:None))
    engine.warm();assert engine.flush_logs()
    thread=engine._logs.thread
    if thread:thread.join(1)
    def cannot_start(*_):raise RuntimeError('synthetic thread exhaustion')
    monkeypatch.setattr(threading.Thread,'start',cannot_start)
    result=engine.predict(dict(state='synthetic',questions=Q,deadline_at_ms=int(time.time()*1000)+4000))
    assert result['loads']==1 and not engine.lock.locked()
    assert engine.status()['log_failures']>=1 and engine.status()['log_dropped']>=1
    assert engine.close_logs()


def test_watchdog_setup_failure_releases_model_owner_lock(tmp_path,monkeypatch):
    engine=LayaEngine(tmp_path,{},factory=lambda:None)
    def unavailable():raise RuntimeError('synthetic resource exhaustion')
    monkeypatch.setattr(engine,'watchdog',unavailable)
    engine.warm()
    assert not engine.lock.locked()
    with pytest.raises(Fault,match='runtime_error'):
        engine.predict(dict(state='synthetic',questions=Q,deadline_at_ms=int(time.time()*1000)+4000))
    assert not engine.lock.locked() and engine.close_logs()
