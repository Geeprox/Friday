"""网络适配回归仅使用回环 UDP 和临时凭据，不连接真实摄像头。"""

import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import threading

import pytest

from homeplane.embodied.camera_udp_relay import Relay, capture_environment, read_state


def test_relay_rejects_foreign_target_bad_token_and_short_packets():
    relay = Relay('127.0.0.2', b'a'*32, 0)
    try:
        packet = b'FRU1'+b'a'*32+socket.inet_aton('127.0.0.2')+struct.pack('!H', 32108)+b'probe'
        assert relay.request(packet) == (32108, b'probe')
        assert relay.request(packet[:20]) is None
        assert relay.request(packet[:4]+b'b'*32+packet[36:]) is None
        assert relay.request(packet[:36]+socket.inet_aton('127.0.0.3')+packet[40:]) is None
        assert relay.request(packet[:40]+b'\x00\x16'+packet[42:]) is None
    finally:
        relay.close()


def test_capture_relay_is_private_and_camera_scoped(tmp_path, monkeypatch):
    state = dict(version=1, token='ab'*32, host='host.docker.internal', port=18747,
                 camera_id='synthetic', camera_ip='127.0.0.2')
    path = tmp_path/'camera-udp-relay.json'
    path.write_text(json.dumps(state))
    path.chmod(0o644)
    with pytest.raises(ValueError, match='private_state'):
        read_state(path)
    path.chmod(0o600)
    selected = dict(id='synthetic', device=dict(local_ip='127.0.0.2'))
    library = tmp_path/'test.so'
    library.write_bytes(b'not loaded')
    monkeypatch.setattr(socket, 'gethostbyname', lambda _: '127.0.0.1')
    env = capture_environment(tmp_path, selected, {}, library)
    assert env['LD_PRELOAD'] == str(library)
    assert env['FRIDAY_RELAY_CAMERA_IP'] == '127.0.0.2'
    assert capture_environment(tmp_path, dict(id='other'), {}, library) == {}
    selected['device']['local_ip'] = '127.0.0.3'
    with pytest.raises(ValueError, match='address_changed'):
        capture_environment(tmp_path, selected, {}, library)


@pytest.mark.skipif(sys.platform != 'linux' or not shutil.which('cc'), reason='需要 Linux C 编译器')
def test_native_fortify_receive_preserves_alternate_camera_reply_port(tmp_path):
    library = tmp_path/'tunnel.so'
    source = Path(__file__).resolve().parents[2]/'native/PortableVision/camera_udp_tunnel.c'
    subprocess.run(['cc', '-shared', '-fPIC', '-O2', '-pthread', '-Wall', '-Wextra', '-Werror',
                    str(source), '-ldl', '-o', str(library)], check=True)
    camera = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    camera.bind(('127.0.0.2', 0))
    camera.settimeout(.1)
    reply = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    reply.bind(('127.0.0.2', 0))
    relay = Relay('127.0.0.2', b'a'*32, 0)
    stop = threading.Event()
    def serve():
        while not stop.is_set():
            relay.poll(.01)
            try:
                data, peer = camera.recvfrom(4096)
                reply.sendto(data.upper(), peer)
            except socket.timeout:
                pass
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    # 直接调用 SDK 使用的 fortify ABI，而不是只检查 Python recvfrom。
    code = '''
import ctypes as c, socket, struct, sys
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
s.sendto(b'probe',('127.0.0.2',int(sys.argv[1])))
s.setblocking(True)
lib=c.CDLL(None, use_errno=True)
receive=lib.__recvfrom_chk
receive.argtypes=[c.c_int,c.c_void_p,c.c_size_t,c.c_size_t,c.c_int,c.c_void_p,c.POINTER(c.c_uint)]
receive.restype=c.c_ssize_t
buffer=c.create_string_buffer(64);address=c.create_string_buffer(16);size=c.c_uint(16)
count=receive(s.fileno(),buffer,64,64,0,address,c.byref(size))
assert count==5 and buffer.raw[:count]==b'PROBE'
assert socket.inet_ntoa(address.raw[4:8])=='127.0.0.2'
assert struct.unpack('!H',address.raw[2:4])[0]==int(sys.argv[2])
s.close()
'''
    try:
        env = dict(os.environ, LD_PRELOAD=str(library), FRIDAY_RELAY_IP='127.0.0.1',
                   FRIDAY_RELAY_PORT=str(relay.listener.getsockname()[1]),
                   FRIDAY_RELAY_TOKEN=(b'a'*32).hex(), FRIDAY_RELAY_CAMERA_IP='127.0.0.2')
        subprocess.run([sys.executable, '-c', code, str(camera.getsockname()[1]),
                        str(reply.getsockname()[1])], env=env, check=True, timeout=8)
        assert relay.counts['sent'] == relay.counts['received'] == 1
    finally:
        stop.set()
        thread.join(2)
        camera.close()
        reply.close()
        relay.close()
