"""Fixture checks only; these do not qualify real gateway hardware."""
import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_geek_upstream_source_is_exact_pinned_mit_copy():
    source = ROOT / 'workers/geek/vendor/client.ts'
    assert hashlib.sha256(source.read_bytes()).hexdigest() == 'b383efcdf8bfdf21d4ed7c36aeb1eda1953a4e479effdb2ffdc7a4c27044c7a4'
    assert 'MIT License' in (source.parent / 'LICENSE').read_text()


@pytest.mark.skipif(shutil.which('node') is None, reason='Node unavailable')
def test_geek_readonly_policy_and_native_fingerprint():
    script = r'''
import assert from 'node:assert/strict';
import {validateCall,normalizeURL,rawRecord} from './workers/geek/policy.mjs';
for(const method of ['setGraph','deleteGraph','changeGraphConfig','setVarValue','createVar','setProperties','action','getGraph/../setGraph']) assert.throws(()=>validateCall(method,{}),/unsupported/);
for(const method of ['getDevList','getGraphList','getVarScopeList']) {validateCall(method,{});assert.throws(()=>validateCall(method,{execute:true}),/invalid_params/);}
validateCall('getGraph',{id:'opaque'});validateCall('getVarList',{scope:'global'});
assert.throws(()=>validateCall('getGraph',{id:'x',enable:false}));
assert.throws(()=>validateCall('getGraph',{id:''}));
assert.equal(normalizeURL('http://192.168.50.1:8086/#/'),'http://192.168.50.1:8086');
for(const url of ['https://example.com','http://127.0.0.1','http://192.168.50.1/api','http://user:pass@192.168.50.1','http://192.168.50.1/?redirect=x']) assert.throws(()=>normalizeURL(url));
const raw=' { "id": 1, "result": {"unknown": "opaque", "nodes": []} }\n';
const record=rawRecord(Buffer.from(raw));assert.equal(record.bytes.toString(),raw);
assert.notEqual(record.sha256,rawRecord(JSON.stringify(record.parsed)).sha256);
assert.throws(()=>rawRecord(Buffer.alloc(4194305)),/too_large/);
'''
    result = subprocess.run(['node', '--input-type=module', '-e', script], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(shutil.which('node') is None, reason='Node unavailable')
def test_geek_failure_diagnostics_use_fixed_codes_only():
    script = r'''
import assert from 'node:assert/strict';
import {classifyFailure} from './workers/geek/diagnostics.mjs';
assert.equal(classifyFailure(new Error('Gateway connection closed (1006)')), 'gateway_closed_connection');
assert.equal(classifyFailure(new Error('Gateway authentication timed out')), 'authentication_timeout');
assert.equal(classifyFailure(Object.assign(new Error('private-host-secret'), {code:'ECONNREFUSED'})), 'connection_refused');
assert.equal(classifyFailure(new Error('secret passcode and household response')), 'check_failed');
assert.equal(classifyFailure(new Error('ZKP verification failed for round two')), 'authentication_handshake_failed');
assert.equal(classifyFailure(new Error('Unexpected response type: 8')), 'unexpected_handshake_frame');
assert.equal(classifyFailure(new Error('Unexpected response type: 4')), 'gateway_error_frame');
'''
    result = subprocess.run(['node', '--input-type=module', '-e', script], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(not (ROOT / 'workers/geek/node_modules/ws').exists(), reason='Minimal Geek dependencies not installed')
def test_geek_guard_is_inside_actual_upstream_call_path():
    script = r'''
import assert from 'node:assert/strict';
import {loadClient} from './workers/geek/load-client.mjs';
const GatewayClient=await loadClient();const client=new GatewayClient();
await assert.rejects(client.callApi('setGraph',{}),/unsupported_read_method/);
await assert.rejects(client.callApi('getGraph',{id:'x',enable:true}),/invalid_params/);
await assert.rejects(client.callApi('getDevList',{}),/Secure Session not established/);
await client.close();
'''
    result = subprocess.run(['node', '--input-type=module', '-e', script], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
