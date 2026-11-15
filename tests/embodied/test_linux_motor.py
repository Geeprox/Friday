"""Linux 云台 ABI 与步进边界只用合成内存、时钟和回调，不加载 SDK。"""

import ctypes as c
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from homeplane.embodied import local_motor as module


def test_linux_abi_is_distinct_and_unknown_architecture_is_rejected():
    digest,anchor,send=module.abi_profile('Linux','x86_64')
    assert digest==module.LINUX_SHA256 and anchor==0x86de0 and send==0x895a0
    assert module.abi_profile('Darwin','arm64')!=(digest,anchor,send)
    with pytest.raises(RuntimeError,match='unsupported_motor_host'):
        module.abi_profile('Linux','aarch64')
    with pytest.raises(RuntimeError,match='motor_sdk_hash_changed'):
        module.verify_binary(b'synthetic not SDK','Linux','x86_64')


@pytest.fixture
def motor(monkeypatch):
    clock=[10.];monkeypatch.setattr(module.time,'monotonic',lambda:clock[0])
    outer=c.create_string_buffer(128);session=c.create_string_buffer(64)
    c.c_uint32.from_buffer(outer,0x40).value=4
    c.c_void_p.from_buffer(outer,0x38).value=c.addressof(session)
    c.c_uint32.from_buffer(session).value=4
    value=module.LocalMotor.__new__(module.LocalMotor)
    value.camera=SimpleNamespace(_c_instance=c.c_void_p(c.addressof(outer)))
    value.send=Mock(return_value=0);value.last=-10
    return value,clock,outer,session


@pytest.mark.parametrize('direction,operation',[('left',1),('right',2),('up',3),('down',4)])
def test_direction_uses_existing_session_and_never_claims_physical_motion(motor,direction,operation):
    value,clock,outer,session=motor
    result=value.step(direction,expires_mono=11)
    args=value.send.call_args.args
    assert args[0]==c.addressof(session) and args[1]==0x112
    assert args[2]==('{"operation":'+str(operation)+'}').encode() and args[3]==len(args[2])
    assert result['physical_movement_verified'] is False
    with pytest.raises(RuntimeError,match='motor_rate_limited'):
        value.step(direction,expires_mono=11)
    assert value.send.call_count==1


@pytest.mark.parametrize('state',['expired','camera','session','direction'])
def test_bad_state_never_reaches_native_send(motor,state):
    value,clock,outer,session=motor
    if state=='camera':c.c_uint32.from_buffer(outer,0x40).value=1
    if state=='session':c.c_uint32.from_buffer(session).value=1
    with pytest.raises((ValueError,RuntimeError)):
        value.step('other' if state=='direction' else 'left',expires_mono=9 if state=='expired' else 11)
    value.send.assert_not_called()
