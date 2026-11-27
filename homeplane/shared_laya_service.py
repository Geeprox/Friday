"""通过 ARL 宿主声明本机 Laya 的进程身份、单实例和服务生命周期。"""

import argparse
import hashlib
import json
import os
import signal
import struct
import threading
from pathlib import Path

from arl.contracts import AppPolicy, Fault
from arl.extension_contracts import metadata_policies
from arl.identity import process_identity
from arl.service import Runtime

from .runtime_log import RuntimeLog
from .shared_laya import HOME, PREDICT, STATUS
from .shared_laya_engine import LayaEngine


class ExecutablePeers:
    """Host-installed local compute grants; kernel PID/executable, no RPC enrollment.

This admits the configured interpreter, not arbitrary claimed app names. It is
not an OS sandbox against the account owner. No credentials/device tools exist.
"""
    def __init__(self, clients):
        self.clients = clients

    def __call__(self, connection, app):
        try:
            entry = self.clients.get(app)
            if entry is None:
                return None
            pid = struct.unpack("i", connection.getsockopt(0, 2, 4))[0]
            peer = process_identity(pid)
            path = Path(entry["executable"]).resolve(strict=True)
            if (peer["uid"] == os.geteuid() and peer["executable"] == str(path)
                    and hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"]):
                return app
        except (OSError, Fault, KeyError, TypeError):
            pass
        return None


class SharedRuntime(Runtime):
    def __init__(self, home, engine, clients):
        self.engine = engine
        policies = metadata_policies()
        for name in clients:
            policies[name] = AppPolicy(frozenset(), frozenset(), 0, 0, 0,
                                       methods=frozenset({STATUS, PREDICT, "health", "ready"}))
        super().__init__(home, policies=policies, providers={}, identity_verifier=ExecutablePeers(clients),
                         execute_jobs=False, host_attachment=self, shutdown_grace=5)

    def start(self, runtime):
        # Called only after ARL acquired its account-wide instance lock.
        def supervise():
            while not self.stopping.is_set():
                if self.engine.agent is None:
                    self.engine.warm()
                if self.stopping.wait(30):
                    return
        self.loader = threading.Thread(target=supervise, daemon=True)
        self.loader.start()

    def begin_shutdown(self):
        # Hold the ARL lifetime lock while inference or checkpoint loading lives.
        self.loader.join()
        self.engine.close()

    def close(self, runtime_finished=None):
        pass

    def dispatch(self, request, verified_app=None, peer_binding=None):
        if type(request) is dict and request.get("method") in {STATUS, PREDICT}:
            if (set(request) != {"protocol", "app", "method", "params"} or request["protocol"] != "arl/1"
                    or verified_app is None or verified_app != request["app"]
                    or request["app"] not in self.policies):
                raise Fault("app_identity_unverified")
            if self.stopping.is_set():
                raise Fault("not_ready")
            if request["method"] == STATUS:
                if request["params"] != {}:
                    raise Fault("invalid_arguments")
                return self.engine.status()
            return self.engine.predict(request["params"])
        return super().dispatch(request, verified_app, peer_binding)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, default=HOME)
    args = parser.parse_args()
    os.environ.update(HF_HUB_OFFLINE="1", HF_HUB_DISABLE_PROGRESS_BARS="1", TOKENIZERS_PARALLELISM="false")
    path = args.home / "host.json"
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise Fault("unsafe_home")
    config = json.loads(path.read_text())
    log = RuntimeLog(args.home / "logs")
    engine = LayaEngine(config["model"], config["model_sha256"], log=log)
    runtime = SharedRuntime(args.home, engine, config["clients"])
    def stop(*_):
        threading.Thread(target=runtime.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        runtime.run()
    except Fault as exc:
        if exc.code != "already_running":
            raise


if __name__ == "__main__":
    main()
