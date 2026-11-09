"""Prepare only the pinned Oboe source; never builds or starts audio devices."""
from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def verify(folder, manifest):
    for name, digest in manifest["files"].items():
        file = folder / name
        if (not file.is_file() or file.is_symlink()
                or hashlib.sha256(file.read_bytes()).hexdigest() != digest):
            raise ValueError("pinned_oboe_source_mismatch")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true", help="Download fixed public source, no build or execution")
    args = parser.parse_args()
    manifest = json.loads((ROOT / "native/KTVTV/dependencies/oboe.json").read_text())
    target = ROOT / ".artifacts/embodied-toolchain" / ("oboe-" + manifest["version"])
    if target.exists():
        verify(target, manifest)
        print(json.dumps({"version": manifest["version"], "verified": True, "audio_started": False}))
        return
    if not args.prepare:
        print(json.dumps({"version": manifest["version"], "verified": False, "prepare_required": True}))
        return
    if (manifest["repository"] != "https://github.com/google/oboe.git"
            or manifest["version"] != "1.11.0"
            or manifest["commit"] != "b115f47593969fd67a21e9f63640ffef749b5067"):
        raise ValueError("reviewed_oboe_release_required")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".fetch-" + secrets.token_hex(4))
    def git(*args):
        subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], check=True,
                       capture_output=True, timeout=180)
    try:
        git("clone", "--depth", "1", "--filter=blob:none", "--no-checkout", "--branch", manifest["version"],
            manifest["repository"], str(temporary))
        git("-C", str(temporary), "sparse-checkout", "set", "--no-cone", "/include/", "/src/", "/CMakeLists.txt", "/LICENSE")
        git("-C", str(temporary), "checkout", "--detach", manifest["commit"])
        verify(temporary, manifest)
        temporary.rename(target)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    print(json.dumps({"version": manifest["version"], "verified": True, "audio_started": False}))


if __name__ == "__main__":
    main()
