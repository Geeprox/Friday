"""Prepare explicitly selected local music into cached KTV stems."""
from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from homeplane.ktv_accompaniment import RUNTIME, prepare, worker
from homeplane.ktv_startup import installed_workspace


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--synthetic-input", action="store_true")
    parser.add_argument("--worker-request", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--installed-root", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker_request:
        workspace = installed_workspace(Path(__file__).resolve().parents[1], args.installed_root)
        return worker(args.worker_request, workspace=workspace)
    if args.installed_root:
        parser.error("installed_root_requires_worker_request")
    if not all((args.input, args.cache_dir, args.report)):
        parser.error("--input, --cache-dir and --report are required")
    cancel = threading.Event()
    for name in (signal.SIGTERM, signal.SIGINT):
        signal.signal(name, lambda *_: cancel.set())
    try:
        value = prepare(args.input, args.cache_dir, args.report, runtime=RUNTIME,
                        timeout=args.timeout, cancel=cancel, simulated=args.synthetic_input)
    except Exception:
        value = {"schema_version": 1, "status": "unavailable", "code": "invalid_request",
                 "instrumental_path": None, "vocals_path": None, "audio_uploaded": False}
    print(json.dumps(value, ensure_ascii=False, allow_nan=False))
    return 0 if value["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
