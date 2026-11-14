"""可选的家庭内网单目标中继。只转发 TLS，模型 Key 仍由 NAS 保管。"""
import argparse
import ipaddress
import select
import socket
import socketserver
import threading

TARGET = "api.openai.com"
DESTINATION = TARGET.encode("ascii") + b":443"


def upstream_socket(proxy_port):
    if proxy_port is None:
        return socket.create_connection((TARGET, 443), timeout=8)
    peer = socket.create_connection(("127.0.0.1", proxy_port), timeout=8)
    try:
        peer.sendall(b"CONNECT " + DESTINATION + b" HTTP/1.1\r\nHost: " + DESTINATION + b"\r\n\r\n")
        with peer.makefile("rb", buffering=0) as response:
            status = response.readline(256).split(None, 2)
            if len(status) < 2 or status[1] != b"200":
                raise OSError("upstream_refused")
            for _ in range(25):
                line = response.readline(1024)
                if line == b"\r\n":
                    return peer
                if not line or not line.endswith(b"\r\n"):
                    break
        raise OSError("upstream_header_invalid")
    except BaseException:
        peer.close()
        raise


class Tunnel(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(5)
        with self.request.makefile("rb", buffering=0) as request:
            line = request.readline(256).strip().split()
            if (len(line) != 3 or line[:2] != [b"CONNECT", DESTINATION] or
                    line[2] not in (b"HTTP/1.0", b"HTTP/1.1")):
                self.request.sendall(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
                return
            for _ in range(25):
                header = request.readline(1024)
                if header == b"\r\n":
                    break
                if not header or not header.endswith(b"\r\n"):
                    return
            else:
                return
        try:
            with upstream_socket(self.server.proxy_port) as upstream:
                self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                self.request.settimeout(15)
                upstream.settimeout(15)
                peers = {self.request: upstream, upstream: self.request}
                while peers:
                    ready, _, _ = select.select(list(peers), [], [], 235)
                    if not ready:
                        return
                    for source in ready:
                        destination = peers[source]
                        data = source.recv(32768)
                        if data:
                            destination.sendall(data)
                        else:
                            peers.pop(source)
                            destination.shutdown(socket.SHUT_WR)
        except (OSError, ValueError):
            return


class Relay(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, local, nas, proxy_port=None):
        self.nas, self.proxy_port = nas, proxy_port
        self.slots = threading.BoundedSemaphore(3)
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", required=True, type=private)
    parser.add_argument("--nas", required=True, type=private)
    parser.add_argument("--port", type=int, default=19148)
    parser.add_argument("--local-proxy-port", type=int, help="以 Mac 已有的回环 HTTP 代理为上游")
    args = parser.parse_args()
    if args.bind == args.nas or not 1024 <= args.port <= 65535:
        parser.error("中继和 NAS 需要不同地址，并使用普通用户端口")
    if args.local_proxy_port is not None and not 1024 <= args.local_proxy_port <= 65535:
        parser.error("回环代理端口无效")
    with Relay((args.bind, args.port), args.nas, args.local_proxy_port) as relay:
        relay.serve_forever()


if __name__ == "__main__":
    main()
