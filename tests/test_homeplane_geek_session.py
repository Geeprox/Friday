"""Explicitly simulated workers/providers. No household PIN or gateway calls."""
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from homeplane.api import create_app
from homeplane.geek_session import REVISION, GeekSession
from homeplane.geek_setup import GeekSetup

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "http://127.0.0.1:8740"


def simulated_snapshot(root):
    """Synthetic raw data exercising production contracts; no gateway is used."""
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    directory = root / ("snapshot-" + str(uuid4()))
    directory.mkdir(mode=0o700)
    records = []
    for method, value in [
        ("getGraphList", [{"id": "simulated-graph", "userData": {"name": "simulated <unsafe>\nname", "secret": "never-return-this"}, "enable": True}]),
        ("getGraph", {"id": "simulated-graph", "nodes": [{"unknown": "opaque-sensitive-node"}]}),
    ]:
        raw = json.dumps({"id": 42, "result": value}).encode()
        file = f"{len(records)+1:03d}-{method}.json"
        path = directory / file
        path.write_bytes(raw)
        path.chmod(0o600)
        records.append({"method": method, "file": file, "raw_sha256": hashlib.sha256(raw).hexdigest(),
                        "content_sha256": "b" * 64, "byte_count": len(raw)})
    manifest = {"schema_version": 1, "simulated": False, "provider": "oh-my-sage-readonly",
                "graph_semantics": "opaque", "fingerprint": "a" * 64,
                "observed_at": "2026-10-01T04:00:00+00:00", "records": records,
                "graphs": {"simulated-graph": "b" * 64}}
    path = directory / "manifest.json"
    path.write_text(json.dumps(manifest))
    path.chmod(0o600)
    return directory


class Input:
    closed = False

    def close(self):
        self.closed = True


class Process:
    def __init__(self):
        self.stdout = asyncio.StreamReader()
        self.stdin = Input()
        self.returncode = None
        self.ended = asyncio.Event()
        self.terminations = 0

    def emit(self, **fields):
        record = {"schema_version": 1, "kind": "status", "provider": "oh-my-sage-readonly",
                  "revision": REVISION, "status": "online", "simulated": False,
                  "writes_attempted": False, "graph_semantics": "opaque",
                  "checked_at": "2026-10-01T04:00:00+00:00", "reconnects": 0,
                  "device_count": 3, "graph_count": 2, "scope_count": 1,
                  "fingerprint": "a" * 64, "changes": {"added": 0, "removed": 0, "modified": 0},
                  "session_seconds": 0, **fields}
        self.stdout.feed_data(json.dumps(record).encode() + b"\n")

    def terminate(self):
        self.terminations += 1
        self.returncode = 0
        self.ended.set()
        self.stdout.feed_eof()

    def kill(self):
        self.terminate()

    async def wait(self):
        await self.ended.wait()
        return self.returncode


async def settle():
    for _ in range(8):
        await asyncio.sleep(0)


@pytest.fixture
def setup(tmp_path):
    value = GeekSetup(tmp_path / "private")
    value.save("SIMULATED-private-pin")
    return value


@pytest.mark.asyncio
async def test_same_connection_reuses_process_changes_persist_and_stop_is_exact(setup):
    processes = []

    async def launch(url, output):
        process = Process()
        processes.append(process)
        return process

    session = GeekSession(setup, launcher=launch)
    assert (await session.connect("http://192.168.50.1:8086"))["status"] == "starting"
    first = processes[0]
    first.emit(changes={"added": 0, "removed": 0, "modified": 1})
    await settle()
    assert session.status()["status"] == "online"
    assert (await session.connect("http://192.168.50.1:8086"))["session_live"]
    assert len(processes) == 1
    first.emit()
    await settle()
    assert session.status()["last_change"]["modified"] == 1
    await session.connect("http://192.168.50.2:8086")
    assert len(processes) == 2 and first.returncode == 0 and first.stdin.closed
    await session.close()
    assert processes[1].returncode == 0 and session.status()["status"] == "stopped"
    assert not session.status()["session_live"]
    assert setup.available() and setup.secret.read_text() == "SIMULATED-private-pin"


@pytest.mark.asyncio
async def test_expired_or_dead_worker_never_keeps_online_status(setup):
    process = Process()
    now = [1.0]

    async def launch(*_):
        return process

    session = GeekSession(setup, launcher=launch, clock=lambda: now[0])
    await session.connect("http://192.168.50.1:8086")
    process.emit()
    await settle()
    now[0] = 182
    assert session.status()["status"] == "stale"
    assert session.status()["inventory_stale"]
    process.terminate()
    await settle()
    assert session.status()["status"] == "worker_stopped"
    assert not session.status()["session_live"]
    await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["secret", "write", "oversize", "flood", "incomplete"])
async def test_worker_output_faults_close_owned_process_and_never_return_secrets(setup, fault):
    process = Process()

    async def launch(*_):
        return process

    session = GeekSession(setup, launcher=launch)
    await session.connect("http://192.168.50.1:8086")
    if fault == "oversize":
        process.stdout.feed_data(b"x" * 4097 + b"\n")
    elif fault == "secret":
        process.emit(passcode="SIMULATED-sensitive")
    elif fault == "write":
        process.emit(writes_attempted=True)
    elif fault == "incomplete":
        process.emit(fingerprint=None)
    else:
        for _ in range(13):
            process.emit()
    await settle()
    assert session.status()["status"] == "worker_fault"
    assert not session.status()["session_live"] and process.returncode == 0
    assert "SIMULATED-sensitive" not in json.dumps(session.status())
    await session.close()


@pytest.mark.asyncio
async def test_handshake_failure_reports_no_false_pin_expiry_and_no_parent_retry(setup):
    calls = []

    async def launch(*_):
        process = Process()
        calls.append(process)
        process.emit(status="blocked", failure_phase="authenticate", failure_code="gateway_error_frame")
        process.stdout.feed_eof()
        return process

    session = GeekSession(setup, launcher=launch)
    await session.connect("http://192.168.50.1:8086")
    await settle()
    assert session.status()["status"] == "blocked"
    assert session.status()["failure_code"] == "gateway_error_frame"
    assert session.status()["credential_available"]
    assert len(calls) == 1 and not session.status()["session_live"]
    await session.close()


@pytest.mark.asyncio
async def test_cancelled_launch_never_admits_late_process(setup):
    ready = asyncio.Event()
    process = Process()

    async def launch(*_):
        await ready.wait()
        return process

    session = GeekSession(setup, launcher=launch)
    pending = asyncio.create_task(session.connect("http://192.168.50.1:8086"))
    await settle()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    ready.set()
    await settle()
    assert process.returncode == 0 and process.stdin.closed
    assert not session.status()["session_live"]
    await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["http://127.0.0.1", "http://169.254.1.1", "http://192.168.1.1/?x=y", "http://192.168.1.1/#/", "https://example.com"])
async def test_credential_destination_is_exact_private_origin_without_launch(setup, url):
    async def forbidden(*_):
        raise AssertionError("must not launch")
    session = GeekSession(setup, launcher=forbidden)
    assert (await session.connect(url))["status"] == "unsupported_gateway_address"
    assert not session.live()


def test_private_evidence_retention_is_bounded_and_symlink_pin_refused(setup, tmp_path):
    setup.evidence.mkdir()
    for _ in range(4):
        root = setup.evidence / ("session-" + str(uuid4()))
        root.mkdir()
        os.close(setup.lease(root))
    untouched = setup.evidence / "user-files"
    untouched.mkdir()
    setup.prune()
    assert len([p for p in setup.evidence.glob("session-*") if p.is_dir()]) == 1 and untouched.exists()
    assert len(list(setup.evidence.glob("session-*.lock"))) == 1
    setup.secret.unlink()
    original = tmp_path / "pin"
    original.write_text("SIMULATED-private-pin")
    original.chmod(0o600)
    setup.secret.symlink_to(original)
    assert not setup.available()


def test_api_is_readonly_explicit_and_settings_pin_change_close_session(tmp_path):
    processes = []

    async def launch(*_):
        process = Process()
        processes.append(process)
        process.emit()
        return process

    app = create_app(db_path=tmp_path / "trace.sqlite", settings_path=tmp_path / "settings.json", geek_launcher=launch)
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get("/api/settings/geek/session").json()["status"] == "stopped"
        contract = client.get("/api/contracts").json()["geek_session_status"]
        assert contract == json.loads((ROOT / "schemas/GeekSessionStatus.json").read_text())
        assert contract["properties"]["writes_attempted"]["const"] is False
        assert not processes
        headers = {"origin": ORIGIN}
        client.put("/api/settings", json={"geek_url": "http://192.168.50.1:8086"}, headers=headers)
        client.post("/api/settings/geek/credential", json={"passcode": "SIMULATED-private-pin"}, headers=headers)
        assert client.post("/api/settings/geek/connect", headers={"origin": "https://evil.invalid"}).status_code == 403
        assert client.post("/api/settings/geek/connect", headers=headers).status_code == 200
        view = client.get("/api/settings").json()["geek_session"]
        assert view["status"] == "online" and view["session_live"]
        assert not view["permanent_login_verified"] and not view["raw_graphs_public"]
        assert "SIMULATED-private-pin" not in json.dumps(view)
        assert client.get("/api/snapshot").json()["mode"] == "simulator"
        assert client.post("/api/settings/geek/probe", headers=headers).json()["status"] == "online"
        assert len(processes) == 1
        client.put("/api/settings", json={"ha_instance": "SIMULATED"}, headers=headers)
        assert processes[0].returncode is None
        client.put("/api/settings", json={"geek_url": "http://192.168.50.2:8086"}, headers=headers)
        assert processes[0].returncode == 0
        client.post("/api/settings/geek/connect", headers=headers)
        client.post("/api/settings/geek/credential", json={"passcode": "SIMULATED-new-pin"}, headers=headers)
        assert processes[1].returncode == 0
        client.post("/api/settings/geek/connect", headers=headers)
        assert client.post("/api/settings/geek/disconnect", headers=headers).json()["status"] == "stopped"
        assert processes[2].returncode == 0
        client.post("/api/settings/geek/connect", headers=headers)
    assert processes[3].returncode == 0  # Lifespan only reaps its own worker.


@pytest.mark.skipif(shutil.which("node") is None, reason="Node unavailable")
def test_actual_worker_sampler_loop_raw_graphs_fingerprints_reconnect_and_cancel(tmp_path):
    script = r'''
import assert from 'node:assert/strict';
import {readFile, mkdir, readdir, lstat} from 'node:fs/promises';
import {join} from 'node:path';
import {fingerprint,pollInventory,saveInventory} from './workers/geek/session-reader.mjs';
import {watchSession} from './workers/geek/session-watch.mjs';
const output=process.argv[1];await mkdir(output,{mode:0o700});
let rpc=0, edited=false, rawGraph;
const calls=[];
const provider={onRawResponse:null,async callApi(method,params){
 calls.push({method,params});
 const values={getDevList:{devList:{'SIMULATED-device':{name:'SIMULATED device'}}},getGraphList:[{id:'SIMULATED-rule'}],getGraph:{id:'SIMULATED-rule',unknown_vendor_field:{'never-discard':true},nodes:[{opaque:edited?'modified':'original'}]},getVarScopeList:['global'],getVarList:{unknown:true}};
 const value=values[method];const raw=` { "id": ${++rpc}, "result": ${JSON.stringify(value)} }\n`;
 if(method==='getGraph')rawGraph=raw;
 this.onRawResponse(Buffer.from(raw));return value;
}};
const first=await pollInventory(provider), exact=rawGraph;
const again=await pollInventory(provider);
assert.equal(first.fingerprint,again.fingerprint); // New RPC id is not an edit.
assert.notEqual(first.records[2].raw_sha256,again.records[2].raw_sha256);
assert.equal(first.records[2].bytes.toString(),exact);
await saveInventory(output,first);
const root=(await readdir(output)).find(p=>p.startsWith('snapshot-'));
assert.equal(await readFile(join(output,root,first.records[2].file),'utf8'),exact);
assert.equal((await lstat(join(output,root,first.records[2].file))).mode&0o777,0o600);
assert.equal(fingerprint({z:1,a:{b:2,c:3}}),fingerprint({a:{c:3,b:2},z:1}));
edited=true;const changed=await pollInventory(provider);assert.notEqual(changed.graphs['SIMULATED-rule'],first.graphs['SIMULATED-rule']);
for(let i=0;i<5;i++)await saveInventory(output,changed);
assert.equal((await readdir(output)).filter(p=>p.startsWith('snapshot-')).length,4);
assert(calls.every(({method})=>['getDevList','getGraphList','getGraph','getVarScopeList','getVarList'].includes(method)));
await assert.rejects(pollInventory({onRawResponse:null,async callApi(){return {devList:{}};}}),/raw_response_unavailable/);

// This executes the production watch loop with explicit synthetic providers.
let created=0, auths=0, polls=0, saved=0;const statuses=[], pauses=[];
await watchSession({clientFactory:()=>{created++;return{async connect(){},async authenticate(pin){assert.equal(pin,'SIMULATED-pin');auths++;},async close(){}};},passcode:'SIMULATED-pin',output,signal:new AbortController().signal,
 emit:async(status,fields)=>statuses.push({status,...fields}),save:async()=>{saved++;},wait:async ms=>{pauses.push(ms);},
 poll:async()=>{if(++polls===1)return first;throw Error('API call timeout: getGraph');}});
assert.equal(created,4);assert.equal(auths,4);assert.equal(saved,1);assert.equal(statuses.at(-1).status,'blocked');
assert.deepEqual(pauses.filter(ms=>ms!==60000),[1000,5000,15000]);

let denied=0;const failures=[];
await watchSession({clientFactory:()=>{denied++;return{async connect(){},async authenticate(){throw Error('Unexpected response type: 4');},async close(){}};},passcode:'SIMULATED-pin',output,signal:new AbortController().signal,emit:async(s,f)=>failures.push({s,...f}),wait:async()=>{throw Error('must not retry PIN');}});
assert.equal(denied,1);assert.equal(failures.at(-1).failure_code,'gateway_error_frame');

let cycle=0, delayedSaved=0;const abort=new AbortController(), resumed=[];
await watchSession({clientFactory:()=>({async connect(){},async authenticate(){},async close(){}}),passcode:'SIMULATED-pin',output,signal:abort.signal,
 emit:async(s,f)=>{resumed.push({s,...f});if(s==='online'&&f.reconnects===1)abort.abort();},wait:async()=>{},save:async()=>{},poll:async()=>{cycle++;if(cycle===2)throw Error('Secure Session not established');return cycle===1?first:changed;}});
const last=resumed.findLast(r=>r.s==='online');assert.equal(last.gap,true);assert.equal(last.changes.modified,1);
const cancel=new AbortController();let resolveLate, enter;const entered=new Promise(resolve=>enter=resolve);
const pending=watchSession({clientFactory:()=>({async connect(){},async authenticate(){},async close(){}}),passcode:'SIMULATED-pin',output,signal:cancel.signal,emit:async()=>{},save:async()=>{delayedSaved++;},poll:()=>{enter();return new Promise(resolve=>resolveLate=resolve);}});
await entered;cancel.abort();resolveLate(first);await pending;assert.equal(delayedSaved,0);
console.log(JSON.stringify({simulated:true,provider:'SIMULATED doubles',raw_graph_exact:true,reconnect_budget:3,no_pin_retry:true,late_poll_discarded:true,device_writes:0}));
'''
    result = subprocess.run(["node", "--input-type=module", "-e", script, str(tmp_path / "SIMULATED-evidence")],
                            cwd=ROOT, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node unavailable")
def test_actual_watch_loop_daily_snapshot_refresh_and_separate_outage_budgets():
    script = r'''
import assert from 'node:assert/strict';
import {watchSession} from './workers/geek/session-watch.mjs';
const snapshot={fingerprint:'a'.repeat(64),graphs:{'SIMULATED-rule':'b'.repeat(64)},device_count:1,graph_count:1,scope_count:1};
const factory=()=>({async connect(){},async authenticate(){},async close(){}});
let now=0,saves=0,online=0;const daily=new AbortController();
await watchSession({clientFactory:factory,passcode:'SIMULATED-pin',output:'SIMULATED-unused',signal:daily.signal,
 clock:()=>now,poll:async()=>snapshot,save:async()=>{saves++;},wait:async()=>{now+=86400000;},
 emit:async(s,f)=>{if(s==='online'&&++online===2){assert.equal(f.session_seconds,86400);daily.abort();}}});
assert.equal(saves,2);assert.equal(online,2);
let created=0,polls=0;const separate=new AbortController();
await watchSession({clientFactory:()=>{created++;return factory();},passcode:'SIMULATED-pin',output:'SIMULATED-unused',signal:separate.signal,
 poll:async()=>{if(++polls%2===0)throw Error('API call timeout: getGraph');return snapshot;},save:async()=>{},wait:async()=>{},
 emit:async(s,f)=>{if(s==='online'&&f.reconnects===4)separate.abort();}});
assert.equal(created,5); // Four separate recovered outages, not one exhausted budget.
let auths=0,after=0;const blocked=[];
await watchSession({clientFactory:()=>({async connect(){},async authenticate(){if(++auths>1)throw Error('WebSocket is not open: readyState 3 (CLOSED)');},async close(){}}),
 passcode:'SIMULATED-pin',output:'SIMULATED-unused',signal:new AbortController().signal,poll:async()=>{if(++after>1)throw Error('API call timeout: getGraph');return snapshot;},
 save:async()=>{},wait:async()=>{},emit:async(s,f)=>blocked.push({s,...f})});
assert.equal(auths,2);assert.equal(blocked.at(-1).failure_phase,'authenticate');
console.log(JSON.stringify({simulated:true,daily_raw_refresh:true,separate_outages_recover:true,rejected_handshake_not_retried:true,device_writes:0}));
'''
    result = subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def test_opaque_inventory_projection_preserves_hashes_without_raw_or_secret(tmp_path):
    from homeplane.geek_inventory import read_inventory

    root = tmp_path / 'private'
    directory = simulated_snapshot(root)
    result = read_inventory(root, 'a' * 64, 1)
    graph = result['graphs'][0].model_dump()
    assert graph['id'] == 'simulated-graph'
    assert graph['name'] == 'simulated <unsafe>name'
    assert graph['source_enabled'] is True and graph['normalization'] == 'opaque'
    assert not graph['writable'] and graph['readable']
    assert graph['raw_sha256'] == hashlib.sha256((directory / '002-getGraph.json').read_bytes()).hexdigest()
    assert 'opaque-sensitive-node' not in str(result) and 'never-return-this' not in str(result)


@pytest.mark.parametrize('fault', ['raw_mutation', 'public_file', 'symlink', 'path_escape', 'wrong_graph', 'count', 'missing_fingerprint', 'snapshot_budget'])
def test_inventory_refuses_inconsistent_or_unsafe_evidence(tmp_path, fault):
    from homeplane.geek_inventory import read_inventory

    root = tmp_path / 'private'
    directory = simulated_snapshot(root)
    path = directory / 'manifest.json'
    manifest = json.loads(path.read_text())
    expected, count = 'a' * 64, 1
    if fault == 'raw_mutation':
        (directory / '002-getGraph.json').write_text('{"result":{}}')
    elif fault == 'public_file':
        (directory / '002-getGraph.json').chmod(0o644)
    elif fault == 'symlink':
        raw = directory / '002-getGraph.json'
        other = tmp_path / 'other'
        raw.rename(other)
        raw.symlink_to(other)
    elif fault == 'path_escape':
        manifest['records'][1]['file'] = '../outside.json'
    elif fault == 'wrong_graph':
        raw = json.dumps({'result': {'id': 'different-simulated-graph', 'nodes': []}}).encode()
        (directory / '002-getGraph.json').write_bytes(raw)
        manifest['records'][1].update(raw_sha256=hashlib.sha256(raw).hexdigest(), byte_count=len(raw))
    elif fault == 'count':
        count = 2
    elif fault == 'missing_fingerprint':
        expected = 'c' * 64
    else:
        for _ in range(4):
            simulated_snapshot(root)
    path.write_text(json.dumps(manifest))
    with pytest.raises((ValueError, OSError)):
        read_inventory(root, expected, count)


@pytest.mark.asyncio
async def test_session_inventory_does_not_read_old_roots_or_report_stale_as_current(setup):
    process = Process()

    async def launch(url, output):
        simulated_snapshot(output)
        return process

    session = GeekSession(setup, launcher=launch)
    assert (await session.inventory()).status == 'unavailable'
    await session.connect('http://192.168.50.1:8086')
    process.emit(graph_count=1)
    await settle()
    result = await session.inventory()
    assert result.status == 'ready' and len(result.graphs) == 1
    process.terminate()
    await settle()
    assert (await session.inventory()).status == 'stale'
    await session.close()
    assert (await session.inventory()).graphs == []


@pytest.mark.asyncio
async def test_late_local_inventory_is_discarded_after_disconnect(setup, monkeypatch):
    import threading

    from homeplane import geek_inventory

    entered, release = threading.Event(), threading.Event()
    process = Process()

    async def launch(url, output):
        simulated_snapshot(output)
        return process

    original = geek_inventory.read_inventory

    def slow(*args):
        entered.set()
        assert release.wait(2)
        return original(*args)

    session = GeekSession(setup, launcher=launch)
    await session.connect('http://192.168.50.1:8086')
    process.emit(graph_count=1)
    await settle()
    monkeypatch.setattr(geek_inventory, 'read_inventory', slow)
    pending = asyncio.create_task(session.inventory())
    assert await asyncio.to_thread(entered.wait, 1)
    assert (await session.inventory()).status == 'unavailable'
    await session.close()
    release.set()
    result = await pending
    assert result.status == 'stale' and result.graphs == []


def test_inventory_api_uses_shared_contract_and_never_launches_on_get(tmp_path):
    from homeplane.geek_inventory import GeekInventoryView

    async def forbidden(*args):
        raise AssertionError('GET must not start a worker')

    app = create_app(db_path=tmp_path / 'trace.sqlite', settings_path=tmp_path / 'settings.json', geek_launcher=forbidden)
    with TestClient(app, base_url=ORIGIN) as client:
        view = client.get('/api/settings/geek/inventory').json()
        assert view['status'] == 'unavailable' and view['graphs'] == []
        schema = client.get('/api/contracts').json()['geek_inventory']
        assert schema == GeekInventoryView.model_json_schema() == json.loads((ROOT / 'schemas/GeekInventory.json').read_text())
        assert schema['properties']['raw_graphs_public']['const'] is False
