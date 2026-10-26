"""Fetch the exact public OpenCV Zoo artifacts already pinned by Friday."""
import hashlib
import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from homeplane.recognition.artifacts import SFACE_SHA256, YUNET_SHA256  # noqa: E402

MODELS = ROOT / "data/embodied/models"
RECORDS = [
    ("face_detection_yunet", "face_detection_yunet_2023mar.onnx", YUNET_SHA256),
    ("face_recognition_sface", "face_recognition_sface_2021dec.onnx", SFACE_SHA256),
]


def main():
    MODELS.mkdir(parents=True, exist_ok=True)
    report = []
    for folder, name, expected in RECORDS:
        target = MODELS / name
        if target.is_symlink():
            raise ValueError("model_symlink_refused")
        if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == expected:
            report.append(dict(model=name, verified=True, downloaded=False))
            continue
        url = f"https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/{folder}/{name}"
        partial = target.with_suffix(".partial")
        digest, size = hashlib.sha256(), 0
        try:
            with urllib.request.urlopen(url, timeout=45) as response, partial.open("wb") as output:
                while chunk := response.read(65536):
                    size += len(chunk)
                    if size > 80 * 1024 * 1024:
                        raise ValueError("model_size_budget")
                    digest.update(chunk)
                    output.write(chunk)
            if digest.hexdigest() != expected:
                raise ValueError("model_hash_mismatch")
            partial.chmod(0o600)
            os.replace(partial, target)
            report.append(dict(model=name, verified=True, downloaded=True, bytes=size))
        finally:
            partial.unlink(missing_ok=True)
    print(json.dumps(report))


if __name__ == "__main__":
    main()
