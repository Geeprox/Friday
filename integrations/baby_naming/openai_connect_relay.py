"""可选的家庭内网单目标中继。只转发 TLS，模型 Key 仍由 NAS 保管。"""
import argparse
import ipaddress
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import secrets
import select
import socket
import socketserver
import threading
import time

TARGET = "api.openai.com"
DESTINATION = TARGET.encode("ascii") + b":443"
IDLE_SECONDS = 300
CONNECT_SECONDS = 8
TRACE = re.compile(rb"[0-9a-f]{16}")


def upstream_socket(proxy_port):
    if proxy_port is None:
        return socket.create_connection((TARGET, 443), timeout=8)
    peer = socket.create_connection(("127.0.0.1", proxy_port), timeout=CONNECT_SECONDS)
    try:
        peer.sendall(b"CONNECT " + DESTINATION + b" HTTP/1.1\r\nHost: " + DESTINATION + b"\r\n\r\n")
        deadline = time.monotonic() + CONNECT_SECONDS
        header = bytearray()
        while len(header) < 8192:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("upstream_connect_timeout")
            peer.settimeout(remaining)
            byte = peer.recv(1)
            if not byte:
                break
            header.extend(byte)
            if header.endswith(b"\r\n\r\n"):
                status = bytes(header).split(b"\r\n", 1)[0]
                if re.fullmatch(rb"HTTP/1\.[01] 200(?: [^\r\n]*)?", status):
                    return peer
                raise OSError("upstream_refused")
        raise OSError("upstream_header_invalid")
    except (OSError, ValueError):
        peer.close()
        raise


class Tunnel(socketserver.BaseRequestHandler):
    def handle(self):
        start = time.monotonic()
        self.trace = None
        connection_id = secrets.token_hex(4)
        self.first_close = None
        side = "nas"
        phase = "connect"
        reason = "relay_error"
        try:
            self.read_connect()
            side = "upstream"
            phase = "connect"
            with upstream_socket(self.server.proxy_port) as upstream:
                phase = "tunnel"
                side = "nas"
                self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                side, reason = self.forward(upstream)
        except (OSError, ValueError) as error:
            if side == "upstream" and phase == "connect":
                try:
                    self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
                except OSError:
                    pass
            reason = type(error).__name__
        finally:
            if self.trace:
                self.server.trace_event({
                    "at": round(time.time(), 3), "trace": self.trace, "connection": connection_id,
                    "first_close": self.first_close,
                    "phase": phase, "side": side, "reason": reason, "elapsed_ms": round((time.monotonic() - start) * 1000),
                })

    def read_connect(self):
        self.request.settimeout(5)
        with self.request.makefile("rb", buffering=0) as request:
            line = request.readline(256).strip().split()
            if (len(line) != 3 or line[:2] != [b"CONNECT", DESTINATION] or
                    line[2] not in (b"HTTP/1.0", b"HTTP/1.1")):
                self.request.sendall(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
                raise ValueError("destination_rejected")
            for _ in range(25):
                header = request.readline(1024)
                if header == b"\r\n":
                    return
                if not header or not header.endswith(b"\r\n"):
                    break
                key, _, value = header.partition(b":")
                if key.lower() == b"x-friday-tunnel-id" and TRACE.fullmatch(value.strip()):
                    self.trace = value.strip().decode("ascii")
        raise ValueError("connect_header_invalid")

    def forward(self, upstream):
        names = {self.request: "nas", upstream: "upstream"}
        self.request.settimeout(15)
        upstream.settimeout(15)
        peers = {self.request: upstream, upstream: self.request}
        while peers:
            ready, _, _ = select.select(list(peers), [], [], IDLE_SECONDS)
            if not ready:
                return "relay", "idle"
            for source in ready:
                destination = peers[source]
                try:
                    data = source.recv(32768)
                except OSError as error:
                    return names[source], type(error).__name__
                if not data:
                    self.first_close = self.first_close or names[source]
                    peers.pop(source)
                try:
                    if data:
                        destination.sendall(data)
                    else:
                        destination.shutdown(socket.SHUT_WR)
                except OSError as error:
                    return names[destination], type(error).__name__
        return self.first_close, "eof"


class Relay(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 12

    def __init__(self, local, nas, proxy_port=None, trace_event=None):
        self.nas, self.proxy_port = nas, proxy_port
        self.trace_event = trace_event or (lambda event: None)
        self.slots = threading.BoundedSemaphore(12)
        super().__init__(local, Tunnel)

    def verify_request(self, request, addr):
        return addr[0] == self.nas

    def process_request(self, request, addr):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, addr)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, addr):
        try:
            super().process_request_thread(request, addr)
        finally:
            self.slots.release()

    def handle_error(self, request, addr):
        # 不记录请求头或请求中的字节。
        pass


def private(value):
    addr = ipaddress.IPv4Address(value)
    networks = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    if not any(addr in ipaddress.IPv4Network(network) for network in networks):
        raise argparse.ArgumentTypeError("需要绑定一个明确的家庭内网 IPv4 地址")
    return str(addr)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", required=True, type=private)
    parser.add_argument("--nas", required=True, type=private)
    parser.add_argument("--port", type=int, default=19148)
    parser.add_argument("--local-proxy-port", "--upstream-port", dest="local_proxy_port", type=int,
                        help="以 Mac 已有的回环 HTTP 代理为上游")
    parser.add_argument("--trace-log", type=Path, help="在私有文件中保留连接阶段和关闭方向")
    args = parser.parse_args()
    if args.bind == args.nas or not 1024 <= args.port <= 65535:
        parser.error("中继和 NAS 需要不同地址，并使用普通用户端口")
    if args.local_proxy_port is not None and (not 1024 <= args.local_proxy_port <= 65535 or
                                              args.local_proxy_port == args.port):
        parser.error("回环代理端口无效")
    trace_event = None
    if args.trace_log:
        target = args.trace_log
        if (target.is_symlink() or target.parent.stat().st_mode & 0o077 or
                (target.exists() and (not target.is_file() or target.stat().st_mode & 0o077))):
            parser.error("诊断日志必须位于私有目录")
        logger = logging.getLogger("friday.openai.relay")
        logger.setLevel(logging.INFO)
        logger.addHandler(RotatingFileHandler(target, maxBytes=64 * 1024, backupCount=1, encoding="utf-8"))
        trace_event = lambda event: logger.info(json.dumps(event, separators=(",", ":")))
    with Relay((args.bind, args.port), args.nas, args.local_proxy_port, trace_event) as relay:
        relay.serve_forever()


if __name__ == "__main__":
    main()
