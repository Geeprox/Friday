"""只接受 Friday 内网反向代理的请求。身份选择只用于家庭协作。"""

import argparse
import fcntl
import ipaddress
import json
import os
import re
import secrets
import sys
import threading
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .notebook import Notebook, Rejected, as_json
from .pipeline import REASONING_MODEL, APIError, OpenAI, Pipeline, credential
from .search import DEFAULT_PROFILE, DIRECTIONS, MODEL_PROFILES, ROLES

PREFIX = "/apps/baby-naming/"
COOKIE = "friday_baby_naming"
ASSET_ROOT = Path(__file__).with_name("web")
VENDOR_ROOT = Path(__file__).resolve().parents[2] / "homeplane" / "static" / "vendor"
ASSETS = {"": ("index.html", "text/html; charset=utf-8"),
          "style.css": ("style.css", "text/css; charset=utf-8"),
          "app.js": ("app.js", "application/javascript; charset=utf-8"),
          "icons.svg": ("icons.svg", "image/svg+xml"),
          "jade-card.js": ("jade-card.js", "application/javascript; charset=utf-8"),
          "jade-relief.webp": ("jade-relief.webp", "image/webp")}
VENDOR = frozenset({"vendor/three.module.js", "vendor/three.core.js"})
MAX_BODY = 4096
SESSION = re.compile(r"[a-zA-Z0-9_-]{30,65}")


class App:
    def __init__(self, book, pipeline, origins, proxy_proof, *, worker=True):
        self.book, self.pipeline = book, pipeline
        self.origins = set(origins)
        self.hosts = {urlsplit(origin).netloc for origin in origins}
        self.proxy_proof = proxy_proof
        self.sessions = {}
        self.sessions_lock = threading.Lock()
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.worker = threading.Thread(target=self.run, daemon=True) if worker else None
        self.preference_worker = threading.Thread(target=self.analyze_preferences, daemon=True) if worker else None
        if self.worker:
            self.book.interrupt_running()
            self.worker.start()
            self.preference_worker.start()

    def analyze_preferences(self):
        while not self.stop.wait(25):
            try:
                self.pipeline.analyze_if_due()
            except Exception:
                # 证据分析独立于人工查找和发卡。
                continue

    def run(self):
        while not self.stop.is_set():
            self.wake.clear()
            job = self.book.claim()
            if not job:
                self.wake.wait(2)
                continue
            number, direction = job
            try:
                self.pipeline.execute(number, direction)
                proof = self.book.proof(number)
                audited = next((row["result"]["approved"] for row in proof["receipts"]
                                if row["stage"] == "audit" and "approved" in row["result"]), 0)
                self.book.finish_job(number, "complete", "ready" if audited else "no_approved")
            except APIError as error:
                self.book.finish_job(number, "failed", error.code)
            except Exception:
                # 标准错误和 UI 只显示固定状态，不记录姓名、请求正文或凭据。
                self.book.finish_job(number, "failed", "model_unavailable")

    def session(self, header, role):
        if role not in ROLES:
            return None
        jar = SimpleCookie()
        try:
            jar.load(header)
            name = f"{COOKIE}_{role}"
            token = jar[name].value if name in jar else ""
        except (ValueError, KeyError, CookieError):
            return None
        if not SESSION.fullmatch(token):
            return None
        with self.sessions_lock:
            return role if self.sessions.get(token) == role else None

    def join(self, role):
        token = secrets.token_urlsafe(32)
        with self.sessions_lock:
            if len(self.sessions) >= 256:
                self.sessions.pop(next(iter(self.sessions)))
            self.sessions[token] = role
        return token


class Handler(BaseHTTPRequestHandler):
    server_version = "FridayNames"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(12)

    def log_message(self, *args):
        pass

    @property
    def app(self):
        return self.server.app

    def authorized(self):
        return (self.client_address[0] == "127.0.0.1" and self.headers.get("Host") in self.app.hosts
                and secrets.compare_digest(self.headers.get("X-Friday-Naming-Proxy", ""), self.app.proxy_proof))

    def respond(self, status, value, content_type="application/json; charset=utf-8", cookie=None):
        data = as_json(value).encode() if isinstance(value, dict) else value
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
                         "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if cookie:
            role, token = cookie
            secure = "; Secure" if f'https://{self.headers["Host"]}' in self.app.origins else ""
            self.send_header("Set-Cookie",
                             f"{COOKIE}_{role}={token}; Path={PREFIX}; HttpOnly; SameSite=Strict; Max-Age=2592000{secure}")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def route(self):
        if (len(self.headers.get_all("Host", [])) != 1 or
                len(self.headers.get_all("X-Friday-Naming-Proxy", [])) != 1 or
                not self.authorized() or not self.path.startswith(PREFIX)):
            self.respond(404, {"error": "not_found"})
            return None
        path = urlsplit(self.path)
        if path.query or path.fragment or path.netloc:
            self.respond(404, {"error": "not_found"})
            return None
        return path.path[len(PREFIX):]

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        key = self.route()
        if key is None:
            return
        if key == "health":
            self.respond(200, {"status": "ready"})
        elif key in ASSETS:
            name, media = ASSETS[key]
            self.respond(200, (ASSET_ROOT / name).read_bytes(), media)
        elif key in VENDOR:
            self.respond(200, (VENDOR_ROOT / Path(key).name).read_bytes(), "application/javascript; charset=utf-8")
        else:
            self.respond(404, {"error": "not_found"})

    def read_json(self):
        if self.headers.get("Transfer-Encoding") or self.headers.get_content_type() != "application/json":
            raise Rejected("invalid_request")
        size = self.headers.get("Content-Length", "")
        if not re.fullmatch(r"[0-9]{1,4}", size) or not 0 < int(size) <= MAX_BODY:
            raise Rejected("invalid_request")
        try:
            body = json.loads(self.rfile.read(int(size)))
        except (UnicodeError, ValueError):
            raise Rejected("invalid_request") from None
        if not isinstance(body, dict):
            raise Rejected("invalid_request")
        return body

    def do_POST(self):
        key = self.route()
        if key is None:
            return
        origin = self.headers.get("Origin")
        if origin not in self.app.origins or urlsplit(origin).netloc != self.headers.get("Host"):
            self.respond(403, {"error": "origin_rejected"})
            return
        try:
            data = self.read_json()
            if key == "api/join":
                role = data.get("role")
                if role not in ROLES:
                    raise Rejected("invalid_scope")
                self.respond(200, {"role": role}, cookie=(role, self.app.join(role)))
                return
            role = self.app.session(self.headers.get("Cookie", ""), data.get("role"))
            if not role:
                self.respond(401, {"error": "choose_role"})
                return
            direction = data.get("direction")
            self.app.book.check(role, direction)
            book = self.app.book
            if key == "api/state":
                result = book.state(role, direction)
            elif key == "api/next":
                result = book.assign(role, direction)
            elif key == "api/answer":
                advance = data.get("advance", False)
                if type(advance) is not bool:
                    raise Rejected("invalid_request")
                result = book.answer(role, direction, data.get("task"), data.get("value"), advance=advance)
                result["advanced"] = advance
            elif key == "api/undo":
                result = book.undo(role, direction)
            elif key == "api/name":
                result = book.name_action(role, direction, data.get("id"), data.get("action"))
            elif key == "api/resume":
                result = book.resume(role, direction)
            elif key == "api/search":
                operation = data.get("operation", "enqueue")
                if operation == "set_profile":
                    result = book.set_generation_profile(role, direction, data.get("profile"))
                elif operation == "enqueue":
                    if not self.app.pipeline:
                        raise Rejected("key_unavailable")
                    book.request_generation(role, direction)
                    self.app.wake.set()
                    result = book.state(role, direction)
                else:
                    raise Rejected("invalid_request")
            else:
                self.respond(404, {"error": "not_found"})
                return
            self.respond(200, result)
        except Rejected as error:
            self.respond(409, {"error": str(error)})
        except (ValueError, TypeError, OSError):
            self.respond(400, {"error": "invalid_request"})


def origins(raw):
    values = raw.split(",")
    if not values or any(not re.fullmatch(r"https?://[a-zA-Z0-9.:-]+", value) for value in values):
        raise ValueError("invalid_origins")
    return values


def load_proof(path):
    target = Path(path)
    if not target.is_file() or target.is_symlink() or target.stat().st_mode & 0o077:
        raise ValueError("invalid_proxy_proof")
    value = target.read_text().strip()
    if not SESSION.fullmatch(value):
        raise ValueError("invalid_proxy_proof")
    return value


def api_proxy(raw):
    if not re.fullmatch(r"http://[0-9.]+:[0-9]{2,5}", raw):
        raise argparse.ArgumentTypeError("需要明确的内网 IPv4 中继地址和端口")
    peer = urlsplit(raw)
    try:
        address = ipaddress.IPv4Address(peer.hostname)
        port = peer.port
        ranges = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]
        if not port or not any(address in ipaddress.IPv4Network(network) for network in ranges):
            raise ValueError("需要使用家庭内网地址")
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None
    return raw


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Friday 宝宝起名工作台")
    parser.add_argument("--data", required=True)
    parser.add_argument("--seed")
    parser.add_argument("--key-file")
    parser.add_argument("--model", default=REASONING_MODEL)
    parser.add_argument("--api-proxy", type=api_proxy)
    parser.add_argument("--port", type=int, default=19146)
    parser.add_argument("--origins")
    parser.add_argument("--proxy-proof")
    parser.add_argument("--daily-requests", type=int, default=24)
    parser.add_argument("--decisions-shadow", action="store_true")
    parser.add_argument("--smoke", choices=DIRECTIONS)
    parser.add_argument("--smoke-profile", default=DEFAULT_PROFILE, choices=MODEL_PROFILES)
    parser.add_argument("--replay", action="store_true")
    parser.add_argument("--demo", action="store_true")
    args = parser.parse_args()
    if not 3 <= args.daily_requests <= 200:
        parser.error("daily request limit must be between 3 and 200")
    book = Notebook(Path(args.data).absolute())
    lock = (book.root / "service.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.error("notebook in use")
    if args.seed:
        book.import_history(json.loads(Path(args.seed).read_text()))
    if args.replay:
        reports = []
        for direction in DIRECTIONS:
            context = book.model_context(direction)
            if not context:
                parser.error("replay requires seed")
            reports.append({"direction": direction, "historical": len(context["known"]),
                            "exact_exclusions": len(context["denied"]),
                            "single_parent_likes": sum("like" in row["ratings"].values()
                                                       for row in context["examples"]),
                            "candidate_issued": any(book.assign(role, direction)["task"] for role in ROLES)})
        print(as_json({"replay": reports}))
        return
    if args.demo:
        from .simulation import populate

        populate(book)
    pipeline = None
    if not args.demo:
        try:
            key = credential(args.key_file)
            client = OpenAI(key, args.model, proxy=args.api_proxy)
            deep_model, deep_effort = MODEL_PROFILES["sol-high"]
            high_client = OpenAI(key, deep_model, timeout=220, proxy=args.api_proxy, effort=deep_effort)
            pipeline = Pipeline(book, client, high_client=high_client, daily=args.daily_requests,
                                decisions_shadow=args.decisions_shadow)
        except APIError:
            if args.smoke:
                raise
    if args.smoke:
        if not pipeline:
            parser.error("smoke requires a real API client")
        book.interrupt_running()
        job = book.request_generation("husband", args.smoke, args.smoke_profile)
        if book.claim() != (job, args.smoke):
            parser.error("queued job mismatch")
        try:
            pipeline.execute(job, args.smoke)
            result = book.proof(job)
            succeeded = {row["stage"] for row in result["receipts"] if row["status"] == "complete"}
            approved = any(row["stage"] == "audit" and row["result"].get("approved", 0)
                           for row in result["receipts"])
            okay = {"plan", "generate", "audit"} <= succeeded and approved
            book.finish_job(job, "complete" if okay else "failed", "ready" if okay else "no_approved")
        except APIError as error:
            okay = False
            book.finish_job(job, "failed", error.code)
        print(as_json(book.proof(job)))
        if not okay:
            sys.exit(1)
        return
    if not args.origins or not args.proxy_proof:
        parser.error("origins and proxy proof are required")
    app = App(book, pipeline, origins(args.origins), load_proof(args.proxy_proof), worker=bool(pipeline))
    with ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as server:
        server.app = app
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            app.stop.set()
            app.wake.set()


if __name__ == "__main__":
    try:
        main()
    except APIError as error:
        print(as_json({"error": error.code}), file=sys.stderr)
        sys.exit(1)
