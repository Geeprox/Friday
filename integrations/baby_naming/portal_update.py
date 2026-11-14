"""在现场确认的内网运行目录中加入起名路径，核验失败则回滚。"""

import argparse
import hashlib
import ipaddress
import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

ROUTE = "/apps/baby-naming/"
BACKUP = ".before-baby-naming"
TAG = "baby-naming.mjs"
TILE = {"id": "babynaming", "name": "宝宝起名", "url": ROUTE,
        "desc": "两个人各自看名字，喜欢的再一起留。"}
TILE_ICON = ('<svg class="symbol" viewBox="0 0 100 100" aria-hidden="true">'
             '<path d="M18 26h23c5 0 9 3 9 7v46c0-5-4-8-9-8H18V26Zm64 0H59c-5 0-9 3-9 7v46'
             'c0-5 4-8 9-8h23V26ZM27 39h13m-13 10h13m20-10h13m-13 10h13"/></svg>')
ANCHOR = re.compile(r"const\s+url\s*=\s*new\s+URL\(req\.url,\s*(['\"])http://\1\s*\+\s*host\s*\);")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def strict_file(path):
    if path.resolve() != path or not path.is_file():
        raise ValueError("path_not_canonical")
    return path.read_bytes()


def atomic(path, data, mode=0o600):
    part = path.with_name("." + path.name + ".baby-naming")
    fd = os.open(part, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(part, path)
    finally:
        part.unlink(missing_ok=True)


def patches(server, html, proof, origins, port, lan=None, trusted_proxy=None, forwarded_header=None):
    if TAG in server or ROUTE in html:
        raise ValueError("route_already_present")
    if not origins or any(origin not in ("http://friday.local", "https://friday.local") for origin in origins):
        raise ValueError("lan_origin_required")
    anchors = list(ANCHOR.finditer(server))
    if len(anchors) != 1:
        raise ValueError("route_anchor_changed")
    anchor = anchors[0].group()
    config = {"proof": str(proof), "origins": origins, "port": port}
    if lan:
        network = ipaddress.IPv4Network(lan)
        if network.prefixlen != 24 or not any(network.subnet_of(ipaddress.IPv4Network(block)) for block in
                                             ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")):
            raise ValueError("private_lan_slash_24_required")
        config["lan"] = str(network.network_address).rsplit(".", 1)[0]
    if trusted_proxy:
        if trusted_proxy != "127.0.0.1" or forwarded_header not in ("x-forwarded-for", "x-real-ip"):
            raise ValueError("invalid_local_proxy")
        config.update(trustedProxy=trusted_proxy, forwardedHeader=forwarded_header)
    call = json.dumps(config, ensure_ascii=True)
    server = ("import {babyNamingRoute} from './baby-naming.mjs';\n" +
              server.replace(anchor, anchor + "\n   if(babyNamingRoute(req,res,url," + call + "))return;"))
    matches = list(re.finditer(r"\bconst\s+APPS\s*=\s*", html))
    if len(matches) != 1:
        raise ValueError("portal_tiles_anchor_changed")
    start = matches[0].end()
    apps, length = json.JSONDecoder().raw_decode(html[start:])
    if not isinstance(apps, list) or not apps or any(not isinstance(app, dict) for app in apps):
        raise ValueError("invalid_portal_tiles")
    actual = apps[0]
    if "name" not in actual or not any(key in actual for key in ("url", "href")):
        raise ValueError("portal_tile_fields_changed")
    tile = {key: TILE[key] for key in ("id", "name") if key in actual}
    tile["href" if "href" in actual else "url"] = ROUTE
    for key in ("desc", "description", "summary", "detail"):
        if key in actual:
            tile[key] = TILE["desc"]
    if "icon" in actual:
        tile["icon"] = TILE_ICON
    if "tone" in actual:
        tile["tone"] = "home"
    if "pending" in actual:
        tile["pending"] = False
    html = html[:start] + json.dumps(apps + [tile], ensure_ascii=False) + html[start + length:]
    link = "function urlFor(a){"
    if html.count(link) != 1:
        raise ValueError("portal_navigation_anchor_changed")
    return server, html.replace(link, link + "if(a.id==='babynaming')return a.url;", 1)


def check_http(host, portal_port, service_port, token, portal_address="127.0.0.1", gateway_port=None):
    opener = build_opener(ProxyHandler({}))

    def read(port, path, headers=None, address="127.0.0.1"):
        url = f"http://{address}:{port}{path}"
        with opener.open(Request(url, headers=headers or {}), timeout=8) as response:
            if response.status != 200:
                raise ValueError("route_not_ready")
            return response.read(1_000_000)

    if read(service_port, ROUTE + "health", {"Host": host, "X-Friday-Naming-Proxy": token}) != b'{"status":"ready"}':
        raise ValueError("service_not_ready")
    try:
        read(service_port, ROUTE + "health", {"Host": host})
        raise ValueError("service_exposed_without_gateway")
    except HTTPError as error:
        if error.code != 404:
            raise
    if portal_port:
        native = gateway_port or portal_port
        if ROUTE.encode() not in read(native, "/", {"Host": host}, portal_address):
            raise ValueError("tile_missing")
        if "宝宝起名".encode() not in read(native, ROUTE, {"Host": host}, portal_address):
            raise ValueError("page_missing")
        if portal_address != "127.0.0.1":
            try:
                read(portal_port, ROUTE, {"Host": host})
                raise ValueError("portal_exposed_to_loopback_proxy")
            except HTTPError as error:
                if error.code != 404:
                    raise


def plan(args):
    if args.host != "friday.local" or args.portal_port == args.port:
        raise ValueError("lan_portal_required")
    root = args.runtime
    src = {name: strict_file(root / name) for name in ("server.mjs", "index.html")}
    lan = getattr(args, "lan", None)
    if lan and ipaddress.IPv4Address(args.portal_address) not in ipaddress.IPv4Network(lan):
        raise ValueError("portal_address_not_in_lan")
    revised = patches(src["server.mjs"].decode(), src["index.html"].decode(), args.proof, args.origin, args.port,
                      lan, getattr(args, "trusted_proxy", None), getattr(args, "forwarded_header", None))
    return src, dict(zip(src, (item.encode() for item in revised)))


def process_id(path):
    value = strict_file(path).strip()
    if not re.fullmatch(rb"[1-9][0-9]{1,7}", value):
        raise ValueError("invalid_portal_pid")
    return int(value)


def supervised_worker(runtime):
    # NAS 的现有启动任务会在门户子进程退出五秒后拉起新进程；不停止监督进程。
    supervisor = process_id(runtime / ".service-lock" / "pid")
    worker = process_id(runtime / "worker.pid")
    parent, child = (Path("/proc") / str(pid) for pid in (supervisor, worker))
    for process in (parent, child):
        if process.stat().st_uid != os.getuid():
            raise ValueError("portal_worker_owner")
    parent_args = (parent / "cmdline").read_bytes().strip(b"\0").split(b"\0")
    worker_args = (child / "cmdline").read_bytes().strip(b"\0").split(b"\0")
    status = (child / "status").read_text()
    if (not any(Path(os.fsdecode(arg)) == runtime / "run-nas.sh" for arg in parent_args)
            or len(worker_args) != 3 or Path(os.fsdecode(worker_args[1])) != runtime / "server.mjs"
            or Path(os.fsdecode(worker_args[2])) != runtime.parent
            or not re.search(r"^PPid:\s+" + str(supervisor) + r"$", status, re.M)
            or Path(os.fsdecode(worker_args[0])).name != "node"):
        raise ValueError("portal_worker_identity_changed")
    return supervisor, worker


def preflight_restart(args):
    legacy = args.runtime / "start-nas.sh"
    if legacy.exists():
        strict_file(legacy)
    else:
        strict_file(args.runtime / "run-nas.sh")
        supervised_worker(args.runtime)


def restart(args):
    legacy = args.runtime / "start-nas.sh"
    if legacy.exists():
        strict_file(legacy)
        subprocess.run(["/bin/sh", str(legacy), "restart"], check=True, capture_output=True, timeout=45)
        return
    supervisor, worker = supervised_worker(args.runtime)
    os.kill(worker, signal.SIGTERM)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        time.sleep(.4)
        try:
            parent, arrived = supervised_worker(args.runtime)
            if parent != supervisor:
                raise ValueError("portal_supervisor_changed")
            if arrived != worker:
                status = build_opener(ProxyHandler({})).open(
                    Request(f"http://127.0.0.1:{args.portal_port}/", headers={"Host": args.host}), timeout=3)
                with status as reply:
                    if reply.status == 200:
                        return
        except (OSError, HTTPError):
            continue
    raise ValueError("portal_worker_restart_timeout")


def apply(args):
    src, updates = plan(args)
    preflight_restart(args)
    files = dict(updates, **{TAG: Path(__file__).with_name("portal.mjs").read_bytes()})
    old = {name: args.runtime / (name + BACKUP) for name in src}
    target = {name: args.runtime / name for name in files}
    manifest = args.runtime / ("baby-naming-portal" + BACKUP + ".json")
    if any(path.exists() for path in [*old.values(), target[TAG], manifest]):
        raise ValueError("previous_portal_transaction_exists")
    token = strict_file(args.proof).decode().strip()
    check_http(args.host, None, args.port, token)
    if set(args.expected) != set(src) or any(digest(data) != args.expected[name] for name, data in src.items()):
        raise ValueError("runtime_changed")
    applied, backed_up = [], []
    try:
        for name, data in src.items():
            if strict_file(target[name]) != data:
                raise ValueError("runtime_changed")
            atomic(old[name], data)
            backed_up.append(name)
        for name, data in files.items():
            if name in src and strict_file(target[name]) != src[name]:
                raise ValueError("runtime_changed")
            atomic(target[name], data)
            applied.append(name)
        subprocess.run([args.node, "--check", str(target["server.mjs"])], check=True,
                       capture_output=True, timeout=10)
        restart(args)
        check_http(args.host, args.portal_port, args.port, token, getattr(args, "portal_address", "127.0.0.1"),
                   getattr(args, "gateway_port", None))
        atomic(manifest, json.dumps({"before": {k: digest(v) for k, v in src.items()},
                                     "installed": {k: digest(v) for k, v in files.items()}}).encode())
    except BaseException:
        for name in reversed(applied):
            if target[name].read_bytes() != files[name]:
                raise ValueError("runtime_changed_during_rollback") from None
            if name in src:
                atomic(target[name], src[name])
            else:
                target[name].unlink()
        if applied:
            restart(args)
        for name in backed_up:
            if old[name].read_bytes() == src[name]:
                old[name].unlink()
        raise
    return {"state": "active", "route": ROUTE}


def rollback(args):
    manifest = args.runtime / ("baby-naming-portal" + BACKUP + ".json")
    record = json.loads(strict_file(manifest))
    for name in ("index.html", "server.mjs", TAG):
        if digest(strict_file(args.runtime / name)) != record["installed"][name]:
            raise ValueError("runtime_changed")
    backups = {name: strict_file(args.runtime / (name + BACKUP)) for name in ("index.html", "server.mjs")}
    if any(digest(data) != record["before"][name] for name, data in backups.items()):
        raise ValueError("backup_changed")
    for name, data in backups.items():
        atomic(args.runtime / name, data)
    (args.runtime / TAG).unlink()
    restart(args)
    return {"state": "rolled_back", "retained": "service_and_data"}


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("plan", "apply", "rollback"))
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--proof", type=Path, required=True)
    parser.add_argument("--origin", action="append", required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=19146)
    parser.add_argument("--portal-port", type=int, default=8792)
    parser.add_argument("--node", default="node")
    parser.add_argument("--lan")
    parser.add_argument("--portal-address", default="127.0.0.1")
    parser.add_argument("--gateway-port", type=int)
    parser.add_argument("--trusted-proxy")
    parser.add_argument("--forwarded-header")
    parser.add_argument("--expected", type=Path)
    args = parser.parse_args()
    if args.runtime.resolve() != args.runtime or args.proof.resolve() != args.proof:
        parser.error("canonical absolute paths required")
    if args.action == "plan":
        src, _ = plan(args)
        print(json.dumps({name: digest(data) for name, data in src.items()}))
    elif args.action == "apply":
        if not args.expected:
            parser.error("apply requires hash receipt")
        args.expected = json.loads(strict_file(args.expected))
        print(json.dumps(apply(args)))
    else:
        print(json.dumps(rollback(args)))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error) if isinstance(error, ValueError) else type(error).__name__) from None
