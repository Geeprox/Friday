"""Synthetic startup/recovery definitions; no actual processes or household services."""
import json
from pathlib import Path
from homeplane.ktv_startup import definition
from integrations.ktv_public.launch import owned


def test_launch_agent_reuses_only_existing_api_port_and_fixed_entry(tmp_path):
    value=definition(tmp_path,'192.168.10.5')
    assert value['KeepAlive']=={'SuccessfulExit':False}
    assert value['RunAtLoad'] and value['ThrottleInterval']==3
    assert len(value['Sockets'])==2
    assert {s['SockNodeName'] for s in value['Sockets'].values()}=={'192.168.10.5','127.0.0.1'}
    assert {s['SockServiceName'] for s in value['Sockets'].values()}=={'8769'}
    assert value['ProgramArguments'][-1]=='--launchd-sockets'
    assert not any('token' in s.lower() for s in value['ProgramArguments'])


def test_nas_recovery_refuses_pid_reuse_or_different_config(tmp_path):
    proc=tmp_path/'proc';process=proc/'123';process.mkdir(parents=True)
    entry=tmp_path/'release/supervisor.py';config=tmp_path/'config.json'
    command=process/'cmdline';command.write_bytes(b'python\0'+str(entry).encode()+b'\0--config\0'+str(config).encode()+b'\0')
    assert owned(123,entry,config,proc=proc)
    assert not owned(123,entry.with_name('nas_server.py'),config,proc=proc)
    assert not owned(123,entry,config.with_name('other.json'),proc=proc)
    assert not owned(True,entry,config,proc=proc)
    assert not owned(1,entry,config,proc=proc)


def test_adopted_socket_mismatch_closes_all_descriptors(monkeypatch):
    import ctypes
    import socket
    from homeplane.ktv_startup import activate
    arrays=[]
    class Function:
        def __init__(self,fn):self.fn=fn
        def __call__(self,*args):return self.fn(*args)
    def load(name,descriptors,count):
        array=(ctypes.c_int*1)(10 if name==b'KTVLAN' else 11);arrays.append(array)
        ctypes.cast(descriptors,ctypes.POINTER(ctypes.POINTER(ctypes.c_int)))[0]=ctypes.cast(array,ctypes.POINTER(ctypes.c_int))
        ctypes.cast(count,ctypes.POINTER(ctypes.c_size_t))[0]=1
        return 0
    class Library:
        launch_activate_socket=Function(load)
        free=Function(lambda p:None)
    created=[]
    class Listener:
        family=socket.AF_INET
        def __init__(self,fileno):self.fd=fileno;self.closed=False;created.append(self)
        def getsockname(self):return ('192.168.10.5' if self.fd==10 else '127.0.0.1',8769)
        def getsockopt(self,*args):return socket.SOCK_STREAM
        def close(self):self.closed=True
    monkeypatch.setattr(socket,'socket',Listener)
    good=activate('192.168.10.5',8769,library=Library())
    assert len(good)==2 and not any(s.closed for s in created)
    created.clear()
    import pytest
    with pytest.raises(ValueError,match='launchd_socket_mismatch'):
        activate('192.168.10.6',8769,library=Library())
    assert all(s.closed for s in created)


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []
    def __call__(self):
        return self.now
    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeTimer:
    created = None
    def __init__(self, delay, callback):
        self.delay, self.callback, self.cancelled, self.started = delay, callback, False, False
        self.created.append(self)
    def start(self):
        self.started = True
    def cancel(self):
        self.cancelled = True
    def fire(self):
        self.callback()


def startup_fixture(tmp_path):
    from homeplane.ktv_startup import startup_fingerprint
    tmp_path.chmod(0o700)
    config, public, source = tmp_path/'config.json', tmp_path/'nas.json', tmp_path/'entry.py'
    config.write_text(json.dumps({'token':'private-placeholder-only'}));config.chmod(0o600)
    public.write_text(json.dumps({'coordination':'nas'}));public.chmod(0o600)
    source.write_text('startup_version = 1\n')
    argv = ['serve','--config',str(config),'--public-config',str(public)]
    fingerprint = lambda first, second: startup_fingerprint(first,second,code_paths=[source])
    return config, public, source, argv, fingerprint, tmp_path/'startup-budget.json'


def test_startup_budget_limits_crash_retries_durably_without_real_sleep(tmp_path):
    from homeplane.ktv_startup import guarded_run
    _, _, _, argv, fingerprint, path = startup_fixture(tmp_path)
    clock, calls, events = FakeClock(), [], []
    def main(args):
        calls.append(args)
        raise RuntimeError('private-path and private-placeholder-only must not escape')
    codes = [guarded_run(main,argv,clock=clock,sleep=clock.sleep,emit=events.append,fingerprint_fn=fingerprint) for _ in range(7)]
    assert codes == [1,1,1,1,1,0,0]
    assert len(calls) == 5 and clock.sleeps == [0,1,2,4,8]
    receipt=json.loads(path.read_text())
    assert receipt['attempts'] == 5 and path.stat().st_mode & 0o077 == 0
    assert path.with_suffix('.lock').stat().st_mode & 0o077 == 0
    assert all('private' not in json.dumps(value) for value in events)
    assert 'private-placeholder-only' not in path.read_text() and str(tmp_path) not in path.read_text()
    assert events[-1] == {'status':'startup_blocked','code':'startup_budget_exhausted'}


def test_budget_rearms_only_for_cooldown_private_config_or_source_fix_and_explicit_reset(tmp_path):
    from homeplane.ktv_startup import StartupBudget, guarded_run
    config, public, source, argv, fingerprint, path = startup_fixture(tmp_path)
    clock, events, calls = FakeClock(), [], []
    def failed(args):
        calls.append(1)
        raise ValueError('fixed test failure')
    def run():
        return guarded_run(failed,argv,clock=clock,sleep=clock.sleep,emit=events.append,fingerprint_fn=fingerprint)
    assert [run() for _ in range(6)] == [1,1,1,1,1,0]
    started=json.loads(path.read_text())['window_started']
    clock.now=started+StartupBudget.WINDOW
    assert run() == 1 and json.loads(path.read_text())['attempts'] == 1
    config.write_text(json.dumps({'token':'changed-private-placeholder'}))
    assert run() == 1 and json.loads(path.read_text())['attempts'] == 1
    public.write_text(json.dumps({'coordination':'nas','fixed':True}))
    assert run() == 1 and json.loads(path.read_text())['attempts'] == 1
    source.write_text('startup_version = 2\n')
    assert run() == 1 and json.loads(path.read_text())['attempts'] == 1
    before=len(calls)
    reset=['--reset-startup-budget',*argv[1:]]
    assert guarded_run(failed,reset,clock=clock,sleep=clock.sleep,emit=events.append,fingerprint_fn=fingerprint) == 0
    assert len(calls) == before and json.loads(path.read_text())['attempts'] == 0
    assert events[-1]['code'] == 'explicit_local_reset'
    assert run() == 1


def test_nonserve_actions_never_read_or_write_startup_budget(tmp_path):
    from homeplane.ktv_startup import guarded_run
    _, _, _, argv, _, path = startup_fixture(tmp_path)
    calls=[]
    def forbidden(*args):
        raise AssertionError('nonserve must not fingerprint or account an attempt')
    for action in ('init','check','plan'):
        assert guarded_run(lambda args:calls.append(args) or 17,[action,*argv[1:]],fingerprint_fn=forbidden) == 17
    assert guarded_run(lambda args:calls.append(args) or 17,['serve','--help',*argv[1:]],fingerprint_fn=forbidden) == 17
    assert len(calls) == 4 and not path.exists() and not path.with_suffix('.lock').exists()


def test_unsafe_or_unwritable_budget_stops_before_initialization_and_reset_repairs_only_private_data(tmp_path, monkeypatch):
    from homeplane.ktv_startup import guarded_run
    _, _, _, argv, fingerprint, path = startup_fixture(tmp_path)
    events, calls, clock = [], [], FakeClock()
    main=lambda args:calls.append(args)
    path.write_text('{broken private json');path.chmod(0o600)
    assert guarded_run(main,argv,clock=clock,sleep=clock.sleep,emit=events.append,fingerprint_fn=fingerprint) == 0
    assert calls == [] and events[-1]['code'] == 'startup_budget_unavailable'
    assert guarded_run(main,['--reset-startup-budget',*argv[1:]],clock=clock,emit=events.append,fingerprint_fn=fingerprint) == 0
    assert json.loads(path.read_text())['attempts'] == 0
    path.chmod(0o644)
    assert guarded_run(main,argv,clock=clock,sleep=clock.sleep,emit=events.append,fingerprint_fn=fingerprint) == 0
    path.chmod(0o600)
    before=path.read_bytes()
    def failed_replace(*args):
        raise OSError('private text not emitted')
    monkeypatch.setattr('homeplane.ktv_startup.os.replace',failed_replace)
    assert guarded_run(main,argv,clock=clock,sleep=clock.sleep,emit=events.append,fingerprint_fn=fingerprint) == 0
    assert calls == [] and path.read_bytes() == before
    assert not list(tmp_path.glob('.ktv-startup-*'))
    assert 'private text' not in str(events)


def test_symlink_budget_and_rollback_clock_fail_closed_without_touching_other_files(tmp_path):
    from homeplane.ktv_startup import StartupBudget, StartupBudgetError, guarded_run
    import pytest
    _, _, _, argv, fingerprint, path = startup_fixture(tmp_path)
    other=tmp_path/'other.json';other.write_text('untouched');other.chmod(0o600)
    path.symlink_to(other)
    calls=[]
    assert guarded_run(lambda args:calls.append(args),argv,emit=lambda value:None,fingerprint_fn=fingerprint) == 0
    assert calls == [] and other.read_text() == 'untouched'
    path.unlink()
    clock=FakeClock();budget=StartupBudget(path,'a'*64,clock=clock)
    budget.reserve();clock.now-=1
    with pytest.raises(StartupBudgetError):
        budget.reserve()
    assert json.loads(path.read_text())['attempts'] == 1


def test_stable_server_clears_budget_after_sixty_seconds_and_old_tokens_cannot_clear_new_attempt(tmp_path):
    from homeplane.ktv_startup import StartupBudget
    _, _, _, _, _, path = startup_fixture(tmp_path)
    clock=FakeClock();budget=StartupBudget(path,'a'*64,clock=clock)
    first=budget.reserve()
    clock.now+=59
    assert not budget.mark_stable(first['attempt_id'])
    clock.now+=1
    assert budget.mark_stable(first['attempt_id'])
    assert json.loads(path.read_text())['attempts'] == 0
    second=budget.reserve();clock.now+=60
    assert not budget.mark_stable(first['attempt_id'])
    assert json.loads(path.read_text())['attempt_id'] == second['attempt_id']
    changed=StartupBudget(path,'b'*64,clock=clock).reserve()
    assert not budget.mark_stable(second['attempt_id'])
    assert json.loads(path.read_text())['attempt_id'] == changed['attempt_id']


def test_only_actual_runtime_ready_arms_stability_and_late_cancelled_timers_do_not_reset_crashes(tmp_path):
    from homeplane.ktv_startup import guarded_run, runtime_ready
    _, _, _, argv, fingerprint, path = startup_fixture(tmp_path)
    clock, timers = FakeClock(), []
    FakeTimer.created=timers
    def init_failure(args):
        clock.now+=61
        raise RuntimeError('initialization never reached HTTP ready')
    options={'clock':clock,'sleep':clock.sleep,'timer_factory':FakeTimer,'emit':lambda value:None,'fingerprint_fn':fingerprint}
    assert guarded_run(init_failure,argv,**options) == 1 and timers == []
    def server_then_failure(args):
        runtime_ready()
        assert len(timers) == 1 and timers[0].delay == 60 and timers[0].started
        clock.now+=59;timers[0].fire()
        assert json.loads(path.read_text())['attempts'] == 2
        raise RuntimeError('short running server')
    assert guarded_run(server_then_failure,argv,**options) == 1
    assert timers[0].cancelled
    clock.now+=60;timers[0].fire()
    assert json.loads(path.read_text())['attempts'] == 2
    def healthy(args):
        runtime_ready();clock.now+=60;timers[-1].fire()
        assert json.loads(path.read_text())['attempts'] == 0
    assert guarded_run(healthy,argv,**options) == 0


def test_stability_storage_failure_requests_orderly_stop_and_success_exit(tmp_path):
    from homeplane.ktv_startup import guarded_run, runtime_ready
    _, _, _, argv, fingerprint, path = startup_fixture(tmp_path)
    clock, timers, stopped, events = FakeClock(), [], [], []
    FakeTimer.created=timers
    def main(args):
        runtime_ready(lambda:stopped.append(True))
        path.chmod(0o644);clock.now+=60;timers[-1].fire()
        return 1
    assert guarded_run(main,argv,clock=clock,sleep=clock.sleep,timer_factory=FakeTimer,emit=events.append,fingerprint_fn=fingerprint) == 0
    assert stopped == [True] and events[-1]['code'] == 'startup_budget_unavailable'


class PendingConnection:
    def __init__(self):
        self.closed=False
    def close(self):
        self.closed=True
    def recv(self,*args):
        raise AssertionError('blocked startup may not read or execute requests')


class PendingListener:
    def __init__(self, queue):
        self.queue, self.closed, self.blocking = queue, False, True
    def setblocking(self,value):
        self.blocking=value
    def accept(self):
        if not self.queue:
            raise BlockingIOError()
        value=self.queue.pop(0)
        if isinstance(value,Exception):
            raise value
        return value,('synthetic',0)
    def close(self):
        self.closed=True


def test_exhausted_socket_activation_drains_same_queued_connections_then_exits_success(tmp_path):
    from homeplane.ktv_startup import StartupBudget, drain_pending_listeners, guarded_run
    config, public, _, argv, fingerprint, path = startup_fixture(tmp_path)
    clock=FakeClock();budget=StartupBudget(path,fingerprint(str(config),str(public)),clock=clock)
    for _ in range(5):budget.reserve()
    connections=[PendingConnection(),PendingConnection()]
    queue=[ConnectionAbortedError(),*connections]
    listeners, drained, calls = [], [], []
    def activate_fake(host,port):
        assert (host,port)==('192.168.10.5',8769)
        result=[PendingListener(queue),PendingListener([])];listeners.extend(result);return result
    def drain(host,port):
        drained.append(drain_pending_listeners(host,port,activate_fn=activate_fake))
    command=[*argv,'--host','192.168.10.5','--launchd-sockets']
    for _ in range(2):
        assert guarded_run(lambda args:calls.append(args),command,clock=clock,sleep=clock.sleep,emit=lambda value:None,fingerprint_fn=fingerprint,drain_fn=drain) == 0
    assert calls == [] and drained == [3,0] and queue == []
    assert all(connection.closed for connection in connections)
    assert all(listener.closed and not listener.blocking for listener in listeners)


def test_pending_drain_is_bounded_and_rejects_nonprivate_or_unknown_listener_targets():
    from homeplane.ktv_startup import StartupBudgetError, drain_pending_listeners
    import pytest
    queue=[PendingConnection() for _ in range(10)]
    listener=PendingListener(queue)
    assert drain_pending_listeners('10.20.30.40',8769,activate_fn=lambda *args:[listener],limit=3) == 3
    assert len(queue)==7 and listener.closed
    for host in ('0.0.0.0','169.254.10.5','8.8.8.8'):
        with pytest.raises(StartupBudgetError):
            drain_pending_listeners(host,8769,activate_fn=lambda *args:(_ for _ in ()).throw(AssertionError()))


def test_real_main_dependency_import_failure_is_inside_guard_and_never_reaches_runtime(tmp_path, monkeypatch):
    import builtins
    import scripts.ktv_server as entry
    from homeplane.ktv_startup import guarded_run
    _, _, _, argv, fingerprint, path = startup_fixture(tmp_path)
    original=builtins.__import__
    imports=[]
    def unavailable(name,*args,**kwargs):
        if name == 'homeplane.ktv_service':
            imports.append(name)
            raise ModuleNotFoundError('private dependency details are discarded')
        return original(name,*args,**kwargs)
    monkeypatch.setattr(builtins,'__import__',unavailable)
    clock, events, timers=FakeClock(), [], []
    FakeTimer.created=timers
    assert guarded_run(entry.main,argv,clock=clock,sleep=clock.sleep,timer_factory=FakeTimer,emit=events.append,fingerprint_fn=fingerprint) == 1
    assert imports == ['homeplane.ktv_service'] and timers == []
    assert json.loads(path.read_text())['attempts'] == 1
    assert events == [{'status':'startup_failed','code':'service_startup_failed'}]


def test_storage_blocked_socket_path_also_drains_and_drain_errors_stay_successful_fixed_codes(tmp_path):
    from homeplane.ktv_startup import guarded_run
    _, _, _, argv, fingerprint, path = startup_fixture(tmp_path)
    path.write_text('malformed');path.chmod(0o600)
    events, drained, called=[], [], []
    command=[*argv,'--launchd-sockets']
    assert guarded_run(lambda args:called.append(True),command,emit=events.append,fingerprint_fn=fingerprint,
                       drain_fn=lambda *args:drained.append(args)) == 0
    assert drained == [('127.0.0.1',8769)] and called == []
    def unknown_listener(*args):
        raise RuntimeError('private descriptor error')
    assert guarded_run(lambda args:called.append(True),command,emit=events.append,fingerprint_fn=fingerprint,drain_fn=unknown_listener) == 0
    assert events[-2:] == [{'status':'startup_blocked','code':'startup_listener_unavailable'},
                           {'status':'startup_blocked','code':'startup_budget_unavailable'}]
