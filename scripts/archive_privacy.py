#!/usr/bin/env python3
"""Configure local privacy previews without downloading or starting a model."""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from homeplane.archive_privacy import MODEL_SHA256, PERSON_MODEL_SHA256
from homeplane.archive_privacy_previews import PrivacyPreviews, private_folder


def write_private(path, value):
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as target:
            json.dump(value, target, indent=2)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def copy_verified(source, destination, expected, maximum):
    source = Path(source).absolute()
    if any(p.is_symlink() for p in (source, *source.parents)):
        raise ValueError("privacy_model_source_unavailable")
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    temporary = destination.with_name(destination.name + ".tmp")
    created = False
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= maximum:
            raise ValueError("privacy_model_source_unavailable")
        output = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        created = True
        digest = hashlib.sha256()
        count = 0
        with os.fdopen(output, "wb") as target:
            while chunk := os.read(fd, 1024 * 1024):
                count += len(chunk)
                if count > maximum:
                    raise ValueError("privacy_model_digest_mismatch")
                digest.update(chunk)
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        after = os.fstat(fd)
        if (count != info.st_size or digest.hexdigest() != expected
                or (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                != (info.st_size, info.st_mtime_ns, info.st_ctime_ns)):
            raise ValueError("privacy_model_digest_mismatch")
        os.replace(temporary, destination)
    finally:
        os.close(fd)
        if created:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=REPO / "data/homeplane")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    commands.add_parser("full-frame")
    configure = commands.add_parser("local-model")
    configure.add_argument("--model", type=Path, required=True)
    configure.add_argument("--person-model", type=Path)
    configure.add_argument("--accept-model-terms", action="store_true")
    args = parser.parse_args()
    state = args.state_dir.absolute()
    config = state / "archive-privacy.json"
    try:
        if args.command == "status":
            report = PrivacyPreviews(state / "recording-previews", config=config).configuration_report()
            print(json.dumps({**report, "model_started": False, "recordings_read": False,
                "playback_runtime_available":all(importlib.util.find_spec(name) is not None
                                                 for name in ("av", "cv2", "numpy")),
                "detector_runtime_available":all(importlib.util.find_spec(name) is not None
                                                 for name in ("onnxruntime", "PIL"))}))
            return
        if args.command == "local-model" and not args.accept_model_terms:
            raise ValueError("privacy_model_terms_review_required")
        private_folder(state)
        value = {"version": 1, "mode": "full_frame"}
        if args.command == "local-model":
            models = state / "archive-privacy-models"
            private_folder(models)
            copy_verified(args.model, models / "320n.onnx", MODEL_SHA256, 14 * 1024 * 1024)
            if args.person_model:
                copy_verified(args.person_model, models / "tiny-yolov3-11.onnx", PERSON_MODEL_SHA256, 40 * 1024 * 1024)
            value = {"version": 1, "mode": "local_model", "model_path": str(models / "320n.onnx"),
                     "model_sha256": MODEL_SHA256}
            write_private(models / "source.json", {
                "detector": "NudeNet 3.4.2 bundled 320n.onnx", "sha256": MODEL_SHA256,
                "source": "https://pypi.org/project/nudenet/3.4.2/",
                "license_metadata": "MIT", "bundled_license": "AGPL-3.0",
                "model_terms_acknowledged_by_operator": True,
                "person_model_sha256": PERSON_MODEL_SHA256 if args.person_model else None,
            })
        write_private(config, value)
        print(json.dumps({"configured": True, "mode": value["mode"], "model_started": False,
                          "restart_required": True, "detector_validation": "not_run"}))
    except (OSError, ValueError) as exc:
        allowed = {"privacy_model_source_unavailable", "privacy_model_digest_mismatch",
                   "privacy_model_terms_review_required", "archive_privacy_unavailable"}
        print(json.dumps({"configured": False, "code": str(exc) if str(exc) in allowed
                          else "privacy_configuration_unavailable"}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
