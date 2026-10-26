import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import wave

import pytest
from homeplane.embodied import synthesis


@pytest.fixture
def producer(tmp_path,monkeypatch):
    monkeypatch.setattr(synthesis,'DATA',tmp_path)
    state=SimpleNamespace(mode='valid',calls=[],processes=[])
    async def spawn(*command,**kwargs):
        state.calls.append(command)
        if command[0]=='/usr/bin/say':
            assert command[-2]=='--', 'reply text must not become say options'
            Path(command[4]).write_bytes(b'SYNTHETIC AIFF')
        elif command[2]=='WAVE':
            with wave.open(command[-1],'wb') as output:
                output.setnchannels(1);output.setsampwidth(2);output.setframerate(44100)
                if state.mode!='empty':output.writeframes((b'\x00\x00' if state.mode=='silent' else b'\x00\x10')*4410)
            if state.mode=='truncated':
                p=Path(command[-1]);p.write_bytes(p.read_bytes()[:-100])
        else:
            Path(command[-1]).write_bytes(b'SYNTHETIC encoded M4A')
        process=SimpleNamespace(returncode=0,wait=AsyncMock(return_value=0),kill=Mock())
        state.processes.append(process)
        return process
    monkeypatch.setattr(asyncio,'create_subprocess_exec',spawn)
    return state,tmp_path


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['empty','silent','truncated'])
@pytest.mark.parametrize('file_format',['wav','m4a'])
async def test_success_exit_and_valid_container_do_not_accept_empty_speech(producer,mode,file_format):
    state,root=producer;state.mode=mode
    with pytest.raises(ValueError,match='speech_synthesis_unavailable'):
        await synthesis.synthesize('测试',file_format=file_format)
    assert not list((root/'speech').glob('reply-*'))
    assert not any(call[0]=='/usr/bin/afconvert' and call[2]=='m4af' for call in state.calls)


@pytest.mark.asyncio
async def test_real_pcm_payload_is_retained_privately_and_raw_file_removed(producer):
    _,root=producer
    path,token=await synthesis.synthesize('-o /SYNTHETIC-option-like-reply',file_format='wav')
    assert path.is_file() and token in path.name
    assert path.stat().st_mode & 0o777 == 0o600
    assert list((root/'speech').glob('reply-*'))==[path]
    with wave.open(str(path),'rb') as source:assert source.getnframes()==4410


@pytest.mark.asyncio
async def test_conversion_failure_does_not_leave_partial_audio(producer):
    state,root=producer
    original=asyncio.create_subprocess_exec
    async def fail(*command,**kwargs):
        process=await original(*command,**kwargs)
        if command[0]=='/usr/bin/afconvert':process.wait=AsyncMock(return_value=1)
        return process
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(asyncio,'create_subprocess_exec',fail)
        with pytest.raises(ValueError,match='speech_synthesis_unavailable'):
            await synthesis.synthesize('测试',file_format='wav')
    assert not list((root/'speech').glob('reply-*'))


@pytest.mark.asyncio
async def test_default_m4a_uses_validated_pcm_and_removes_both_intermediates(producer):
    state,root=producer
    path,_=await synthesis.synthesize('测试')
    assert path.suffix=='.m4a' and list((root/'speech').glob('reply-*'))==[path]
    assert state.calls[-1][-2].endswith('.wav')
    assert path.stat().st_mode & 0o777==0o600
