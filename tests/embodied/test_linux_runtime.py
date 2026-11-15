"""Linux 接口与资源限制回归，不启动模型、摄像头或音频。"""

import asyncio
import base64
import io
import json
import runpy
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from PIL import Image
from homeplane.embodied import synthesis
from homeplane.embodied.cpu_review import CpuBudget, CpuReply, CpuWindowProcess, GIB, MEMORY_LIMIT
from homeplane.embodied.cpu_review_worker import images_from_request


@pytest.mark.asyncio
@pytest.mark.parametrize('file_format', ['wav', 'm4a'])
async def test_linux_speech_is_local_and_option_like_text_is_literal(tmp_path, monkeypatch, file_format):
    monkeypatch.setattr(synthesis, 'DATA', tmp_path)
    monkeypatch.setattr(synthesis, 'SPEECH_PLATFORM', 'linux')
    monkeypatch.setattr(synthesis.shutil, 'which', lambda name: '/usr/bin/'+name)
    calls = []
    async def spawn(*command, **kwargs):
        calls.append(command)
        if command[0].endswith('espeak-ng'):
            assert command[-2] == '--'
            Path(command[command.index('-w')+1]).write_bytes(b'SYNTHETIC-WAV')
        elif command[-2] == 'wav':
            with wave.open(command[-1], 'wb') as out:
                out.setnchannels(1);out.setsampwidth(2);out.setframerate(44100)
                out.writeframes(b'\x00\x10'*4410)
        else:
            Path(command[-1]).write_bytes(b'SYNTHETIC-AAC')
        return SimpleNamespace(returncode=0, wait=AsyncMock(return_value=0), kill=Mock(), terminate=Mock(),
                               stdout=SimpleNamespace(read=AsyncMock(return_value=b'')),
                               stdin=SimpleNamespace(close=Mock()))
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    path, _ = await synthesis.synthesize('--output=untrusted', file_format=file_format)
    assert calls[0][-1] == '--output=untrusted'
    assert len(calls) == (2 if file_format == 'wav' else 3)
    assert path.stat().st_mode & 0o777 == 0o600
    assert list((tmp_path/'speech').iterdir()) == [path]


@pytest.mark.asyncio
async def test_missing_linux_voice_dependency_never_spawns(tmp_path, monkeypatch):
    monkeypatch.setattr(synthesis, 'DATA', tmp_path)
    monkeypatch.setattr(synthesis, 'SPEECH_PLATFORM', 'linux')
    monkeypatch.setattr(synthesis.shutil, 'which', lambda name: None)
    spawn = AsyncMock();monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    with pytest.raises(ValueError, match='speech_synthesis_unavailable'):
        await synthesis.synthesize('合成测试')
    spawn.assert_not_awaited()
    assert not list((tmp_path/'speech').iterdir())


def test_cpu_review_budget_and_distinct_memory_protocol():
    assert CpuBudget(16*GIB, 100).reason() == 'memory_capacity'
    assert CpuBudget(32*GIB, 20).reason() == 'memory_pressure'
    assert CpuBudget(32*GIB, 70, cores=4).reason() is None
    owner = CpuWindowProcess()
    data = dict(version=1, result=dict(event='none', observation='合成场景'), peak_rss_bytes=100)
    assert owner.parse_reply(json.dumps(data)).event == 'none'
    assert owner.peak_rss_bytes == 100 and owner.peak_mlx_bytes == 0
    with pytest.raises(ValueError):
        owner.parse_reply(json.dumps(dict(data, peak_rss_bytes=MEMORY_LIMIT+1)))
    assert 'cpu_review_worker' in ' '.join(owner.command())


def test_cpu_windows_reject_reordered_or_nonfinite_time():
    out = io.BytesIO();Image.new('RGB', (600, 300)).save(out, format='JPEG')
    encoded = base64.b64encode(out.getvalue()).decode()
    frames = [dict(t=i, jpeg=encoded) for i in range(4)]
    images, stamps = images_from_request(dict(frames=frames))
    assert stamps == [0,1,2,3] and all(i.size == (256,128) for i in images)
    # Linux 与新主干使用相同的可选姿态复核协议，拒绝宽松布尔值。
    assert images_from_request(dict(frames=frames, review_prone=True))[1] == stamps
    for invalid in (False, 1, 'true', None):
        with pytest.raises(ValueError):
            images_from_request(dict(frames=frames, review_prone=invalid))
    for invalid in [float('nan'), -1, 1]:
        frames[2]['t'] = invalid
        with pytest.raises(ValueError):
            images_from_request(dict(frames=frames))


def test_model_preparation_checks_hashes_without_overwriting(tmp_path):
    scope = runpy.run_path('scripts/embodied/prepare_portable_models.py')
    import hashlib
    manifest = {'synthetic.task': ('https://invalid.test/no-network', hashlib.sha256(b'valid').hexdigest(), 5)}
    assert scope['prepare'](tmp_path, manifest=manifest)['missing'] == ['synthetic.task']
    (tmp_path/'synthetic.task').write_bytes(b'valid')
    assert scope['prepare'](tmp_path, manifest=manifest)['ready']
    (tmp_path/'synthetic.task').write_bytes(b'wrong')
    with pytest.raises(ValueError, match='pin_mismatch'):
        scope['prepare'](tmp_path, download=True, manifest=manifest)
    assert (tmp_path/'synthetic.task').read_bytes() == b'wrong'


def test_linux_build_only_writes_launchers(tmp_path):
    scope = runpy.run_path('scripts/embodied/runtime.py')
    build = scope['build_portable'];build.__globals__['DATA'] = tmp_path
    build.__globals__['sys'] = SimpleNamespace(platform='linux')
    interpreter = tmp_path/'vision-runtime/bin/python';interpreter.parent.mkdir(parents=True)
    interpreter.write_text('synthetic; never execute');interpreter.chmod(0o700)
    assert build()['inference_attempted'] is False
    assert 'portable_vision import main' in (tmp_path/'bin/friday-vision').read_text()
    assert build(sound=True)['camera_opened'] is False
    assert 'portable_sound import main' in (tmp_path/'bin/friday-sound').read_text()


def test_linux_native_sdk_pins_source_and_shared_library(tmp_path, monkeypatch):
    import hashlib
    from homeplane import native_media_worker as module
    home = tmp_path/'sdk';root = home/'lib/python3.12/site-packages/miot'
    native = root/'libs/linux/x86_64/libmiot_camera_lite.so'
    native.parent.mkdir(parents=True);native.write_bytes(b'SYNTHETIC ELF; never load')
    (root/'camera.py').write_text('# 合成 SDK，禁止导入\n')
    manifest = dict(version='2026.9.11', files={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in (native, root/'camera.py')})
    project = tmp_path/'project';(project/'homeplane').mkdir(parents=True)
    (project/'homeplane/native_media_sdk_linux.json').write_text(json.dumps(manifest))
    monkeypatch.setattr(module, 'PROJECT', project)
    monkeypatch.setattr(module, 'sys', SimpleNamespace(platform='linux'))
    monkeypatch.setattr(module.platform, 'machine', lambda:'x86_64')
    monkeypatch.setattr(module.importlib.metadata, 'distributions',
                        lambda **_: [SimpleNamespace(version='2026.9.11', metadata={'Name':'miloco-miot'})])
    assert module.qualify_sdk(home)
    native.write_bytes(b'changed')
    assert not module.qualify_sdk(home)


@pytest.mark.parametrize('version', [1, 2])
def test_linux_memory_honors_container_and_parent_limit(tmp_path, version):
    from homeplane.embodied.temporal_model import GIB, linux_memory
    mem = tmp_path/'meminfo';mem.write_text(f'MemTotal: {64*GIB//1024} kB\nMemAvailable: {48*GIB//1024} kB\n')
    root = tmp_path/'cgroup';root.mkdir()
    parent = root if version == 2 else root/'memory'
    child = parent/'group';child.mkdir(parents=True)
    limit, used = ('memory.max', 'memory.current') if version == 2 else ('memory.limit_in_bytes', 'memory.usage_in_bytes')
    (parent/limit).write_text(str(8*GIB));(parent/used).write_text(str(5*GIB))
    (child/limit).write_text(str(6*GIB));(child/used).write_text(str(GIB))
    membership = tmp_path/'membership';membership.write_text('0::/group\n' if version == 2 else '3:memory:/group\n')
    assert linux_memory(mem,root,membership) == (6*GIB, 3*GIB)
    (child/limit).write_text('invalid')
    with pytest.raises(ValueError):linux_memory(mem,root,membership)
