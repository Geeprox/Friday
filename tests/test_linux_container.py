"""容器发布与验收边界；不调用 Docker、不读取家庭配置。"""
import json
import runpy
from pathlib import Path

import pytest


def builder():
    return runpy.run_path('scripts/build_linux_container.py')


@pytest.mark.parametrize('friday_dirty,arl_dirty,revision', [(True,False,'a'*40),(False,True,'a'*40),(False,False,'b'*40)])
def test_release_rejects_floating_or_modified_sources_before_export(tmp_path, friday_dirty, arl_dirty, revision):
    scope=builder();stage=scope['stage'];root=tmp_path/'Friday';arl=tmp_path/'ARL'
    (root/'docker').mkdir(parents=True);arl.mkdir()
    (root/'docker/runtime-lock.json').write_text(json.dumps({'arl':{'revision':'a'*40}}))
    stage.__globals__['source_state']=lambda path:dict(revision='c'*40 if path==root else revision,
                                                     dirty=friday_dirty if path==root else arl_dirty)
    with pytest.raises(ValueError,match='release_source_not_locked'):
        stage(root,arl,tmp_path/'context',release=True)
    assert not (tmp_path/'context').exists()


def test_verifier_refuses_to_remove_a_container_without_its_owner_label():
    scope=runpy.run_path('scripts/verify_linux_container.py');box=scope['Sandbox']('test-only-image')
    box.container='synthetic-container';calls=[]
    def docker(*args,**kwargs):
        calls.append(args)
        return 'someone-else'
    box.docker=docker
    with pytest.raises(RuntimeError,match='test_container_owner_changed'):box.close()
    assert not any(row[0]=='rm' for row in calls)


def test_verifier_refreshes_ephemeral_ports_after_restart():
    scope=runpy.run_path('scripts/verify_linux_container.py');box=scope['Sandbox']('test-only-image')
    box.container='synthetic-container';calls=[]
    def docker(*args,**kwargs):
        calls.append(args)
        if args[0]=='inspect' and 'Labels' in args[2]:return box.owner
        if args[0]=='inspect' and '.State' in args[2]:return json.dumps({'ExitCode':0,'OOMKilled':False})
        if args[0]=='inspect' and '.Ports' in args[2]:
            return json.dumps({str(port)+'/tcp':[{'HostIp':'127.0.0.1','HostPort':str(port+20000)}] for port in (8740,18745)})
        return ''
    box.docker=docker;box.wait=lambda:None
    box.restart()
    assert box.ports=={8740:28740,18745:38745}
    assert all('compose' not in row for row in calls)
