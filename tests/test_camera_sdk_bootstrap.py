"""首次启动安装的中断、校验和缓存边界；只使用合成归档。"""
import hashlib
import io
import json
import runpy
import tarfile
import zipfile

import pytest


@pytest.fixture
def installer(tmp_path, monkeypatch):
    scope=runpy.run_path('docker/camera_sdk.py')
    ensure=scope['ensure']; state=ensure.__globals__
    folder=tmp_path.resolve()/'resources';(folder/'licenses').mkdir(parents=True)
    license_data=b'synthetic license\n';(folder/'licenses/Xiaomi-Miloco-LICENSE.md').write_bytes(license_data)
    wheel_bytes=io.BytesIO()
    with zipfile.ZipFile(wheel_bytes,'w') as wheel:
        wheel.writestr('miot/camera.py','VALUE = 1\n')
        wheel.writestr('miloco_miot-2026.9.11.dist-info/METADATA','Name: miloco-miot\nVersion: 2026.9.11\n')
    wheel_data=wheel_bytes.getvalue();archive_bytes=io.BytesIO()
    wheel_name='miloco_miot-2026.9.11-py3-none-manylinux_2_28_x86_64.whl'
    with tarfile.open(fileobj=archive_bytes,mode='w:gz') as archive:
        info=tarfile.TarInfo('./'+wheel_name);info.size=len(wheel_data)
        archive.addfile(info,io.BytesIO(wheel_data))
    archive_data=archive_bytes.getvalue()
    lock=dict(version='2026.9.11',wheel=wheel_name,wheel_sha256=hashlib.sha256(wheel_data).hexdigest(),
              archive_sha256=hashlib.sha256(archive_data).hexdigest(),license_sha256=hashlib.sha256(license_data).hexdigest())
    (folder/'camera-sdk-lock.json').write_text(json.dumps(lock))
    state['HERE']=folder
    monkeypatch.setattr(state['subprocess'],'run',lambda *a,**k:None)
    data=tmp_path.resolve()/'data';data.mkdir(mode=0o700)
    def fetch(_,target):target.write_bytes(archive_data)
    return ensure,state,data,lock,fetch


def test_first_install_and_offline_restart_preserve_verified_content(installer):
    ensure,state,data,lock,fetch=installer
    home=ensure(data,fetch=fetch)
    def offline(*_):raise AssertionError('重启不应访问下载源')
    assert ensure(data,fetch=offline)==home
    assert (home/'LICENSE.md').exists() and (home/'NOTICE.txt').exists()
    assert not list((data/'camera-sdk').glob('.install-*'))


def test_interrupted_download_leaves_no_ready_environment_and_retry_succeeds(installer):
    ensure,state,data,lock,fetch=installer
    def interrupted(_,target):
        target.write_bytes(b'partial')
        raise OSError('下载中断')
    with pytest.raises(OSError):ensure(data,fetch=interrupted)
    assert not (data/'camera-sdk'/lock['version']).exists()
    assert ensure(data,fetch=fetch).is_dir()


@pytest.mark.parametrize('field,error',[('archive_sha256','archive_hash'),('wheel_sha256','wheel_hash')])
def test_rejects_mismatched_archive_and_wheel(installer,field,error):
    ensure,state,data,lock,fetch=installer
    lock[field]='0'*64
    (state['HERE']/'camera-sdk-lock.json').write_text(json.dumps(lock))
    with pytest.raises(ValueError,match=error):ensure(data,fetch=fetch)
    assert not (data/'camera-sdk'/lock['version']).exists()


@pytest.mark.parametrize('change',['code','extra','license','pth','interpreter','symlink'])
def test_cache_tampering_fails_without_network_or_replacement(installer,change):
    ensure,state,data,lock,fetch=installer
    home=ensure(data,fetch=fetch);site=home/'lib/python3.12/site-packages'
    if change=='code':(site/'miot/camera.py').write_text('VALUE = 2\n')
    elif change=='extra':(site/'unexpected.pth').write_text('import os\n')
    elif change=='license':(home/'LICENSE.md').write_text('changed')
    elif change=='pth':(site/'friday-runtime.pth').write_text('/tmp\n')
    elif change=='interpreter':
        (home/'bin/python').unlink();(home/'bin/python').write_text('changed')
    else:
        original=site/'miot/camera.py';original.unlink();original.symlink_to('/etc/passwd')
    def offline(*_):raise AssertionError('损坏缓存不能通过静默重装隐藏')
    with pytest.raises(ValueError,match='camera_sdk_'):ensure(data,fetch=offline)


def test_sdk_directory_symlink_is_rejected(installer,tmp_path):
    ensure,state,data,lock,fetch=installer
    target=tmp_path/'outside';target.mkdir(mode=0o700)
    (data/'camera-sdk').symlink_to(target,target_is_directory=True)
    with pytest.raises(ValueError,match='unsafe_directory'):ensure(data,fetch=fetch)
    assert list(target.iterdir())==[]


def test_archive_cannot_supply_a_symlink_as_wheel(installer,tmp_path):
    ensure,state,data,lock,fetch=installer
    archive=tmp_path/'bad.tar.gz'
    with tarfile.open(archive,'w:gz') as stream:
        info=tarfile.TarInfo('./'+lock['wheel']);info.type=tarfile.SYMTYPE;info.linkname='/etc/passwd'
        stream.addfile(info)
    lock['archive_sha256']=state['digest'](archive)
    with pytest.raises(ValueError,match='invalid_archive'):
        state['extract_wheel'](archive,tmp_path/'wheel',lock)


def test_wheel_rejects_path_traversal(installer,tmp_path):
    ensure,state,data,lock,fetch=installer
    wheel=tmp_path/'bad.whl'
    with zipfile.ZipFile(wheel,'w') as stream:stream.writestr('miot/../../evil.py','bad')
    lock['wheel_sha256']=state['digest'](wheel)
    with pytest.raises(ValueError,match='unsafe_wheel'):state['wheel_files'](wheel,lock)


def test_download_hash_failure_has_bounded_retry_and_removes_partial(tmp_path,monkeypatch):
    scope=runpy.run_path('docker/camera_sdk.py');download=scope['download'];state=download.__globals__
    attempts=[]
    class Opener:
        def open(self,*a,**kw):attempts.append(1);return io.BytesIO(b'wrong')
    monkeypatch.setattr(state['urllib'].request,'build_opener',lambda *_:Opener())
    monkeypatch.setattr(state['time'],'sleep',lambda _:None)
    target=tmp_path/'archive'
    lock=dict(archive_url='https://github.com/XiaoMi/xiaomi-miloco/releases/download/vtest/test.tar.gz',
              archive_sha256='0'*64,archive_max_bytes=100)
    with pytest.raises(ValueError,match='archive_hash'):download(lock,target)
    assert len(attempts)==3 and not target.exists()


def test_downgrade_to_http_is_rejected():
    scope=runpy.run_path('docker/camera_sdk.py')
    with pytest.raises(ValueError,match='insecure_redirect'):
        scope['HTTPSRedirect']().redirect_request(None,None,302,'',{},'http://example.invalid/file')
