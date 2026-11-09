"""Frozen SDK import/shape checks in fresh interpreters; no ARL host or models."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def probe(root=None, pin=None, *, python=sys.executable, discover=False):
    code = '''import json,sys
sys.path.insert(0,sys.argv[1])
from homeplane.arl_application import sdk_compatibility
if sys.argv[4]=='discover': sys.path.insert(0,sys.argv[2])
print(json.dumps(sdk_compatibility(vendor_root=None if sys.argv[4]=='discover' else sys.argv[2] or None, expected_manifest_sha256=sys.argv[3] or None)))
'''
    result = subprocess.run([python,'-I','-B','-c',code,str(ROOT),str(root or ''),str(pin or ''),'discover' if discover else 'explicit'],
                            capture_output=True,text=True,timeout=20)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.fixture
def frozen():
    directory = os.environ.get('FRIDAY_TEST_ARL_SDK_INPUT')
    if not directory:
        pytest.skip('Explicit reviewed frozen SDK payload required; no installed-host fallback')
    root = Path(directory).resolve()
    return root, hashlib.sha256((root/'ARL-SDK-MANIFEST.json').read_bytes()).hexdigest()


def test_missing_vendor_is_refused_without_runtime_activity(tmp_path):
    result = probe(tmp_path.resolve())
    assert not result['compatible'] and result['code'] == 'sdk_manifest_missing'
    assert result['registration_attempted'] is False and result['model_attempted'] is False


def test_frozen_sdk_passes_without_construction_or_io(frozen):
    root,pin = frozen
    result = probe(root,pin)
    assert result['compatible'] and result['code'] == 'compatible'
    assert result['source_commit'] == 'a48e8bfd15b9ff7716a6a52a3ef9fcdbe915a585'
    assert not result['denied_events'] and not result['registration_attempted'] and not result['model_attempted']
    assert result['new_threads'] == 0
    assert result['modules'] and all(row['pinned'] for row in result['modules'].values())


def test_frozen_sdk_works_in_explicit_retained_python(frozen):
    python = os.environ.get('FRIDAY_TEST_ARL_SDK_PYTHON')
    if not python:
        pytest.skip('Explicit retained runtime required')
    result = probe(*frozen,python=python)
    assert result['compatible'] and result['code'] == 'compatible'


def test_default_gate_discovers_vendor_and_uses_canonical_pin(frozen):
    result = probe(frozen[0], discover=True)
    assert result['compatible'] and result['manifest_sha256'] == frozen[1]


@pytest.fixture
def edited(frozen,tmp_path):
    root = tmp_path.resolve()/'vendor'
    shutil.copytree(frozen[0],root)
    return root,frozen[1]


@pytest.mark.parametrize('change,code',[
    ('pin','sdk_manifest_pin_mismatch'),('changed','sdk_source_changed'),
    ('extra','sdk_inventory_mismatch'),('missing','sdk_inventory_mismatch'),('symlink','sdk_unsafe_path')])
def test_modified_input_refused_before_import(edited,change,code):
    root,pin = edited
    if change == 'pin': pin='0'*64
    if change == 'changed':
        with (root/'arl/client.py').open('ab') as stream: stream.write(b'\n')
    if change == 'extra': (root/'arl/unreviewed.py').write_text('raise RuntimeError("never imported")')
    if change == 'missing': (root/'arl/application_models.py').unlink()
    if change == 'symlink':
        target=root/'arl/client.py';target.unlink();target.symlink_to(root/'arl/contracts.py')
    result=probe(root,pin)
    assert not result['compatible'] and result['code']==code
    assert result['modules']=={} and result['denied_events']==[]


def repin(root):
    path=root/'ARL-SDK-MANIFEST.json';manifest=json.loads(path.read_text())
    for name,row in manifest['files'].items():
        raw=(root/name).read_bytes()
        row.update(sha256=hashlib.sha256(raw).hexdigest(),size=len(raw),
                   git_blob_sha1=hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest())
    path.write_text(json.dumps(manifest,sort_keys=True,ensure_ascii=False,indent=2)+'\n')
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_same_version_but_missing_scenarios_interface_is_refused(edited):
    root,_ = edited;path=root/'arl/client.py'
    source=path.read_text();assert '*, scenarios=None' in source
    path.write_text(source.replace('*, scenarios=None','*, unsupported_scenarios=None',1))
    result=probe(root,repin(root))
    assert not result['compatible'] and result['code']=='sdk_interface_mismatch'


@pytest.mark.parametrize('operation',[
    'Client(None,None)',
    'ApplicationSession(None,"synthetic","synthetic")',
    'from .service import Runtime; Runtime(None)',
    'Client.rpc(object(), "ready")',
    'subprocess.run(["/usr/bin/true"])',
    'import threading; threading.Thread(target=lambda:None).start()',
    'import _thread; _thread.start_new_thread(lambda:None,())',
    'from pathlib import Path; Path(SENTINEL).write_text("synthetic")',
    'import socket; socket.socket(socket.AF_UNIX).connect(SENTINEL)',
])
def test_import_effect_attempt_is_blocked(edited,tmp_path,operation):
    root,_ = edited;sentinel=tmp_path.resolve()/'no-effects'
    path=root/('arl/application_session.py' if operation.startswith('ApplicationSession(') else 'arl/client.py')
    with path.open('a') as stream:
        stream.write('\nSENTINEL='+repr(str(sentinel))+'\n'+operation+'\n')
    result=probe(root,repin(root))
    assert not result['compatible'] and result['code']=='sdk_side_effect_attempt'
    assert result['denied_events'] and not sentinel.exists()


def test_import_cannot_read_private_configuration(edited,tmp_path):
    root,_ = edited; sentinel=tmp_path.resolve()/'synthetic-private-config'
    sentinel.write_text('synthetic-content-must-not-be-read')
    with (root/'arl/client.py').open('a') as stream:
        stream.write('\nPath('+repr(str(sentinel))+').read_text()\n')
    result=probe(root,repin(root))
    assert not result['compatible'] and result['code']=='sdk_side_effect_attempt'
    assert result['denied_events']==['file.read_outside_sdk_or_stdlib']


def test_unpinned_transitive_module_cannot_fall_back_to_global_sdk(edited):
    root,_ = edited
    with (root/'arl/client.py').open('a') as stream: stream.write('\nimport arl.install_platform\n')
    result=probe(root,repin(root))
    assert not result['compatible'] and result['code']=='sdk_module_not_pinned'
