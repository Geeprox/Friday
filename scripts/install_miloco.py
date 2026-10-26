"""Install the official pinned macOS ARM Miloco release into the workspace."""

import hashlib
import json
import platform
import secrets
import subprocess
import tarfile
import urllib.request
from pathlib import Path

from friday.home import private_json

root = Path(__file__).resolve().parent.parent
if platform.system() != "Darwin" or platform.machine() != "arm64":
    raise SystemExit(
        "This installer targets Apple Silicon. Configure external model/Miloco endpoints on other hosts."
    )
release = json.loads((root / "docs/miloco-release.json").read_text())
deploy = root / "data/deploy"
deploy.mkdir(parents=True, exist_ok=True)
archive = deploy / release["asset"]
if not archive.exists():
    urllib.request.urlretrieve(release["url"], archive)
if hashlib.sha256(archive.read_bytes()).hexdigest() != release["sha256"]:
    raise SystemExit("Miloco release checksum mismatch; installation stopped.")
wheels = deploy / "miloco"
wheels.mkdir(exist_ok=True)
with tarfile.open(archive) as tar:
    tar.extractall(wheels, filter="data")
venv = root / "data/miloco-venv"
if not venv.exists():
    subprocess.run(["uv", "venv", str(venv), "--python", "3.12"], check=True)
subprocess.run(
    [
        "uv",
        "pip",
        "install",
        "--python",
        str(venv / "bin/python"),
        "-r",
        str(root / "docs/miloco-requirements.lock"),
        str(wheels / "miloco_miot-2026.9.11-py3-none-macosx_11_0_arm64.whl"),
        str(wheels / "miloco-2026.9.11-py3-none-any.whl"),
    ],
    check=True,
)
home = root / "data/miloco"
(home / "models").mkdir(parents=True, exist_ok=True)
with tarfile.open(wheels / "miloco-models-2026.9.11.tar.gz") as tar:
    tar.extractall(home / "models", filter="data")
if not (home / "config.json").exists():
    private_json(
        home / "config.json",
        {
            "server": {
                "host": "127.0.0.1",
                "port": 1810,
                "token": secrets.token_urlsafe(32),
                "log_level": "warning",
            },
            "model": {
                "omni": {
                    "label": "Unconfigured",
                    "model": "local-vision",
                    "base_url": "http://127.0.0.1:8811/v1",
                    "api_key": "",
                }
            },
            "schedule": {"enabled": False},
            "scheduler": {"enabled": False},
            "perf": {"enabled": False},
        },
    )
print("Miloco installed and checksum verified.")
