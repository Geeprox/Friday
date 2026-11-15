"""摄像头专用 UDP 转发：用于无法传回局域网发现回复的 Docker 网络。

只使用标准库，不加载摄像头 SDK、不解码媒体、不记录数据包。
监听宿主回环地址；每个请求验证随机凭据且只能发往已登记摄像头。
"""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import os
from pathlib import Path
import secrets
import selectors
import signal
import socket
import struct
import time

MAGIC = b'FRU1'
HEADER = 42
MAX_PAYLOAD = 65465


def read_state(path):
    path = Path(path)
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError('camera_relay_private_state_required')
    state = json.loads(path.read_text())
    if (state.get('version') != 1 or not isinstance(state.get('token'), str)
            or len(state['token']) != 64 or len(bytes.fromhex(state['token'])) != 32
            or type(state.get('port')) is not int or not 1024 <= state['port'] <= 65535
            or state.get('host') != 'host.docker.internal'):
        raise ValueError('camera_relay_invalid_state')
    address = ipaddress.IPv4Address(state['camera_ip'])
    if not address.is_private or address.is_unspecified or address.is_multicast:
        raise ValueError('camera_relay_private_camera_required')
    return state


def capture_environment(private, selected, environment, library):
    """按当前摄像头匹配转发配置；不给其他预览或模型进程注入网络适配。"""
    path = Path(private)/'camera-udp-relay.json'
    if not path.exists():
        return environment
    state = read_state(path)
    if state.get('camera_id') != selected['id']:
        return environment
    if state['camera_ip'] != selected['device'].get('local_ip'):
        raise ValueError('camera_relay_camera_address_changed')
    library = Path(library)
    if not library.is_file():
        raise ValueError('camera_relay_library_missing')
    # 固定 Docker 宿主名，避免私有配置把摄像头流量发往任意域名。
    address = socket.gethostbyname(state['host'])
    if not ipaddress.IPv4Address(address).is_private:
        raise ValueError('camera_relay_private_host_required')
    if environment.get('LD_PRELOAD'):
        raise ValueError('camera_relay_preload_conflict')
    return dict(environment, LD_PRELOAD=str(library),
                FRIDAY_RELAY_IP=address, FRIDAY_RELAY_PORT=str(state['port']),
                FRIDAY_RELAY_TOKEN=state['token'], FRIDAY_RELAY_CAMERA_IP=state['camera_ip'])


class Relay:
    """每个经过认证的客户端独立使用 UDP 端口，保留摄像头回复的源端口。"""

    def __init__(self, camera_ip, token, port):
        self.camera_ip, self.token = camera_ip, token
        self.selector = selectors.DefaultSelector()
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.listener.bind(('127.0.0.1', port))
        self.listener.setblocking(False)
        self.selector.register(self.listener, selectors.EVENT_READ)
        self.clients = {}
        self.running = True
        self.counts = dict(sent=0, received=0, rejected=0)

    def request(self, data):
        if (not HEADER <= len(data) <= HEADER+MAX_PAYLOAD or data[:4] != MAGIC
                or not hmac.compare_digest(data[4:36], self.token)):
            return None
        target = socket.inet_ntoa(data[36:40])
        port = int.from_bytes(data[40:42], 'big')
        if target != self.camera_ip or not 1024 <= port <= 65535:
            return None
        return port, data[HEADER:]

    def poll(self, timeout=.2):
        now = time.monotonic()
        for address, (peer, last) in list(self.clients.items()):
            if now-last > 30:
                self.selector.unregister(peer)
                peer.close()
                del self.clients[address]
        for key, _ in self.selector.select(timeout):
            sock = key.fileobj
            try:
                data, address = sock.recvfrom(65535)
                if sock is self.listener:
                    request = self.request(data)
                    if request is None:
                        self.counts['rejected'] += 1
                        continue
                    if address not in self.clients:
                        if len(self.clients) >= 16:
                            self.counts['rejected'] += 1
                            continue
                        peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                        peer.bind(('', 0))
                        peer.setblocking(False)
                        self.clients[address] = (peer, now)
                        self.selector.register(peer, selectors.EVENT_READ, address)
                    peer, _ = self.clients[address]
                    self.clients[address] = (peer, now)
                    port, payload = request
                    peer.sendto(payload, (self.camera_ip, port))
                    self.counts['sent'] += 1
                elif address[0] == self.camera_ip and len(data) <= MAX_PAYLOAD:
                    packet = (MAGIC+self.token+socket.inet_aton(self.camera_ip)
                              +struct.pack('!H', address[1])+data)
                    self.listener.sendto(packet, key.data)
                    self.clients[key.data] = (sock, now)
                    self.counts['received'] += 1
            except (BlockingIOError, ConnectionError):
                continue

    def close(self):
        self.running = False
        for peer, _ in self.clients.values():
            peer.close()
        self.listener.close()
        self.selector.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--camera')
    parser.add_argument('--port', type=int, default=18747)
    args = parser.parse_args()
    from .paths import camera
    selected = camera(args.camera)
    if args.state.exists():
        state = read_state(args.state)
        if (state.get('camera_id') != selected['id']
                or state['camera_ip'] != selected['device'].get('local_ip')
                or state['port'] != args.port):
            raise ValueError('camera_relay_state_target_changed')
    else:
        address = ipaddress.IPv4Address(selected['device']['local_ip'])
        if not address.is_private or address.is_unspecified or address.is_multicast:
            raise ValueError('camera_relay_private_camera_required')
        if not 1024 <= args.port <= 65535:
            raise ValueError('camera_relay_invalid_port')
        state = dict(version=1, camera_id=selected['id'], camera_ip=str(address),
                     token=secrets.token_hex(32), host='host.docker.internal', port=args.port)
    relay = Relay(state['camera_ip'], bytes.fromhex(state['token']), state['port'])
    try:
        args.state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = args.state.with_name('.'+args.state.name+'-'+secrets.token_hex(8))
        try:
            with os.fdopen(os.open(temporary, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600), 'w') as stream:
                json.dump(dict(state, pid=os.getpid()), stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, args.state)
        finally:
            temporary.unlink(missing_ok=True)
        def stop(*_):
            relay.running = False
        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
        print(json.dumps(dict(ready=True, loopback_only=True, camera_count=1,
                              sdk_loaded=False, media_saved=False)), flush=True)
        while relay.running:
            relay.poll()
    finally:
        relay.close()
        print(json.dumps(relay.counts), flush=True)


if __name__ == '__main__':
    main()
