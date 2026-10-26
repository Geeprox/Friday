"""Account-wide ARL composition for one resident Laya model, independent of UI.

Installed as com.arl.laya.runtime. ARL owns the socket and instance lock; no new
wire protocol, durable speech jobs, caller-selected model paths or fallback.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import queue
import signal
import struct
import threading
import time
import secrets

from arl.contracts import AppPolicy, Fault
from arl.extension_contracts import metadata_policies
from arl.identity import process_identity
from arl.service import Runtime
from homeplane.runtime_log import RuntimeLog, fixed_code
from homeplane.shared_laya import HOME, PREDICT, STATUS, VERSION, validate_input


class DeferredLog:
    """Bounded operational metadata; filesystem writes never hold inference."""
    def __init__(self, sink, *, capacity=64):
        if type(capacity) is not int or not 1 <= capacity <= 64:
            raise ValueError('invalid_log_capacity')
        self.sink = sink
        self.queue = queue.Queue(maxsize=capacity)
        self.gate = threading.Lock()
        self.drained = threading.Event()
        self.drained.set()
        self.thread = None
        self.closed = False
        self.dropped = self.failures = 0

    def record(self, stage, event, **fields):
        with self.gate:
            if self.closed:
                self.dropped = min(self.dropped + 1, 2147483647)
                return
            try:
                self.queue.put_nowait((stage, event, dict(fields)))
            except queue.Full:
                self.dropped = min(self.dropped + 1, 2147483647)
                return
            self.drained.clear()
            if self.thread is None:
                try:
                    self.thread = threading.Thread(target=self._run, daemon=True, name='laya-log-writer')
                    self.thread.start()
                except Exception:
                    self.failures = min(self.failures + 1, 2147483647)
                    # A failed OS thread allocation must not strand metadata
                    # or propagate into the model owner's critical section.
                    if self.thread is None or self.thread.ident is None:
                        self.thread = None
                        while True:
                            try:self.queue.get_nowait()
                            except queue.Empty:break
                            self.queue.task_done()
                            self.dropped = min(self.dropped + 1, 2147483647)
                        if self.queue.unfinished_tasks == 0:self.drained.set()

    def _run(self):
        while True:
            try:
                stage, event, fields = self.queue.get(timeout=.25)
            except queue.Empty:
                with self.gate:
                    if self.queue.empty():
                        self.thread = None
                        return
                continue
            try:
                self.sink.record(stage, event, **fields)
            except Exception:
                self.failures = min(self.failures + 1, 2147483647)
            finally:
                with self.gate:
                    self.queue.task_done()
                    if self.queue.unfinished_tasks == 0:
                        self.drained.set()

    def flush(self, timeout=1):
        return self.drained.wait(max(0, min(timeout, 1)))

    def close(self, timeout=1):
        with self.gate:
            self.closed = True
        return self.flush(timeout)


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


class LayaEngine:
    def __init__(self, model, pins, *, factory=None, clock=time.monotonic, log=None):
        self.model, self.pins, self.factory = Path(model), pins, factory
        self.clock = clock
        self.log = log or RuntimeLog()
        self._logs = DeferredLog(self.log)
        self.lock = threading.Lock()
        self.agent = None
        self.state = "cold"
        self.loads = self.calls = self.failures = 0
        self.last_used = 0.0
        self.last_inference_finished = None
        self.last_call = None
        self.watchdog_timeout = 45
        self.memory = None
        self.previous_wired = None
        self.wired_budget = 0
        self.active_bytes = 0

    def reserve_memory(self, mx):
        # One small resident model, never a global OS wired-limit override. Idle
        # compression on a busy Mac otherwise turns a 300ms call into seconds.
        info = mx.device_info()
        cap = min(1024**3, info['memory_size']//16, info['max_recommended_working_set_size']//8)
        budget = min(cap, (self.model/'model.safetensors').stat().st_size + 64*1024**2)
        self.previous_wired = mx.set_wired_limit(budget)
        self.memory = mx
        self.wired_budget = budget
        mx.set_cache_limit(32*1024**2)

    def release_memory(self):
        if self.memory is not None:
            self.memory.set_wired_limit(self.previous_wired)
            self.memory.clear_cache()
        self.memory = self.previous_wired = None
        self.wired_budget = self.active_bytes = 0

    def watchdog(self):
        # A wedged native model call cannot be cancelled by Python threads.
        # Kill only this dedicated service process; launchd restarts its owner.
        timer = threading.Timer(self.watchdog_timeout, lambda: os._exit(70))
        timer.daemon = True
        timer.start()
        return timer

    def status(self):
        return {"runtime": "arl_shared_laya", "version": VERSION, "pid": os.getpid(),
                "state": self.state, "resident_models": int(self.agent is not None),
                "loads": self.loads, "calls": self.calls, "failures": self.failures,
                "wired_budget_bytes": self.wired_budget, "active_model_bytes": self.active_bytes,
                "last_call": dict(self.last_call) if self.last_call is not None else None,
                "log_pending": self._logs.queue.unfinished_tasks,
                "log_dropped": self._logs.dropped, "log_failures": self._logs.failures,
                "input_persisted": False, "simulated": self.factory is not None}

    def warm(self):
        if not self.lock.acquire(blocking=False):
            return
        correlation = None
        watchdog = None
        try:
            correlation = secrets.token_hex(16)
            watchdog = self.watchdog()
            if self.agent is not None:
                return
            self.state = "loading"
            self._logs.record("arl.worker", "start", upstream="arl", correlation=correlation)
            if self.factory is None:
                if importlib.metadata.version("laya-mlx") != VERSION:
                    raise Fault("offline_dependency")
                for relative, expected in self.pins.items():
                    path = self.model / relative
                    if not path.resolve().is_relative_to(self.model.resolve()) or path.is_symlink():
                        raise Fault("offline_dependency")
                    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                        raise Fault("offline_dependency")
                if "model.safetensors" not in self.pins or "rl_agent_config.json" not in self.pins:
                    raise Fault("offline_dependency")
                from laya_mlx import Agent
                import mlx.core as mx
                self.reserve_memory(mx)
                self.agent = Agent(str(self.model), device="gpu", local_files_only=True)
                # MLX evaluates lazily. Materialize kernels before admitting a
                # user's short-deadline request; this input is fixed and synthetic.
                self.agent.predict("This is a synthetic startup check.", {
                    "command": {"type": "choice", "instructions": "Classify this startup check.",
                                "criteria": {"yes": "a device command", "no": "a startup check"}}})
                mx.clear_cache()
                self.active_bytes = mx.get_active_memory()
            else:
                self.agent = self.factory()
            self.loads += 1
            self.state = "ready"
            self.last_used = self.clock()
            self.last_inference_finished = self.last_used
            self._logs.record("arl.worker", "result", upstream="arl", correlation=correlation)
        except Exception:
            self.agent = None
            self.release_memory()
            self.state = "unavailable"
            self.failures += 1
            self._logs.record("arl.worker", "result", result="unavailable", code="offline_dependency", upstream="arl", correlation=correlation)
        finally:
            try:
                if watchdog is not None:watchdog.cancel()
            finally:self.lock.release()

    def predict(self, params):
        if type(params) is not dict or set(params) != {"state", "questions", "deadline_at_ms"}:
            raise Fault("invalid_arguments")
        validate_input(params["state"], params["questions"])
        deadline = params["deadline_at_ms"]
        admitted = self.clock()
        remaining_ms = deadline - time.time() * 1000 if type(deadline) is int else 0
        if not 0 < remaining_ms <= 5000:
            raise Fault("deadline_exceeded")
        # Convert the wire's wall-clock deadline once. Clock correction must
        # never give an already admitted request extra inference time.
        expires = admitted + remaining_ms / 1000
        if not self.lock.acquire(blocking=False):
            raise Fault("runtime_busy")
        started = admitted
        correlation = None
        watchdog = None
        code, result = "none", "ok"
        idle_ms = model_ms = serialization_ms = None
        model_finished = None
        try:
            started = self.clock()
            correlation = secrets.token_hex(16)
            watchdog = self.watchdog()
            self._logs.record("arl.complete", "start", upstream="arl", correlation=correlation)
            self.check_deadline(expires, deadline)
            if self.agent is None:
                raise Fault("not_ready")
            model_started = self.clock()
            if self.last_inference_finished is not None:
                idle_ms = self.milliseconds(model_started - self.last_inference_finished)
            try:
                reply = self.agent.predict(params["state"], params["questions"])
            finally:
                model_finished = self.clock()
                model_ms = self.milliseconds(model_finished - model_started)
            self.check_deadline(expires, deadline)
            if reply.get("usage", {}).get("truncated") or reply.get("usage", {}).get("options"):
                raise Fault("request_budget_exceeded")
            serialization_started = self.clock()
            try:
                raw = json.dumps(reply, allow_nan=False, ensure_ascii=False)
            finally:
                serialization_ms = self.milliseconds(self.clock() - serialization_started)
            self.check_deadline(expires, deadline)
            if len(raw.encode()) > 16000:
                raise Fault("request_budget_exceeded")
            response = {"answers": reply["answers"], "usage": reply.get("usage", {}),
                        "runtime": "arl_shared_laya", "version": VERSION, "simulated": self.factory is not None,
                        "pid": os.getpid(), "loads": self.loads}
            self.check_deadline(expires, deadline)
            self.calls += 1
            return response
        except Fault as exc:
            code = fixed_code(exc.code)
            result = "timeout" if code == "deadline_exceeded" else "unavailable"
            self.failures += 1
            raise
        except Exception:
            code, result = "runtime_error", "error"
            self.failures += 1
            raise Fault(code) from None
        finally:
            try:
                if watchdog is not None:watchdog.cancel()
                self.last_used = self.clock()
                if model_finished is not None:
                    self.last_inference_finished = model_finished
                # Only bounded operational data enters this immutable snapshot.
                self.last_call = dict(idle_ms=idle_ms, model_ms=model_ms,
                                      serialization_ms=serialization_ms,
                                      total_ms=self.milliseconds(self.last_used - admitted),
                                      result=result, code=code)
                self._logs.record("arl.complete", "result", result=result, code=code, upstream="arl",
                                  elapsed_ms=(self.clock() - started) * 1000, correlation=correlation)
            finally:self.lock.release()

    @staticmethod
    def milliseconds(seconds):
        return max(0, min(int(seconds * 1000), 86400000))

    def check_deadline(self, expires, deadline):
        if self.clock() >= expires or time.time() * 1000 >= deadline:
            raise Fault("deadline_exceeded")

    def flush_logs(self, timeout=1):
        return self._logs.flush(timeout)

    def close_logs(self, timeout=1):
        return self._logs.close(timeout)


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
        with self.engine.lock:
            self.engine.agent = None
            self.engine.release_memory()
            self.engine.state = "stopped"
        self.engine.close_logs(timeout=.25)

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
