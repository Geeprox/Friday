"""Install the reviewed shared ARL Laya owner; never launch an app-owned Agent."""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ["homeplane/shared_laya.py", "homeplane/shared_laya_service.py", "homeplane/runtime_log.py"]
LABEL = "com.arl.laya.runtime"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--review", type=Path)
    args = parser.parse_args()
    hashes = {s:digest(ROOT/s) for s in SOURCES}
    receipt = {"schema":1,"sources":hashes,"label":LABEL,"version":"0.5.0"}
    if not args.apply:
        print(json.dumps(receipt)); return
    review = json.loads(args.review.read_text())
    if review.get("approved") is not True or review.get("sources") != hashes:
        raise ValueError("review_missing_or_stale")
    if importlib.metadata.version("laya-mlx") != "0.5.0":
        raise ValueError("laya_version_mismatch")
    release = ROOT/"data/shared-laya"/hashlib.sha256(json.dumps(hashes,sort_keys=True).encode()).hexdigest()[:16]
    if not release.exists():
        (release/"homeplane").mkdir(parents=True,mode=0o700)
        (release/"homeplane/__init__.py").write_text("")
        for source in SOURCES:
            shutil.copy2(ROOT/source,release/source)
    if any(digest(release/s)!=h for s,h in hashes.items()):
        raise ValueError("release_changed")
    account = Path.home()
    home = account/"Library/Application Support/ARL/laya-v1"
    home.mkdir(mode=0o700,parents=True,exist_ok=True)
    if home.is_symlink() or home.stat().st_mode & 0o077:
        raise ValueError("unsafe_home")
    model = ROOT/"data/embodied/models/decision"
    pins = {str(p.relative_to(model)):digest(p) for p in sorted(model.rglob('*')) if p.is_file() and not p.is_symlink()}
    executable = Path(sys.executable).resolve()
    config = {"schema":1,"model":str(model),"model_sha256":pins,
              "clients":{"friday":{"executable":str(executable),"sha256":digest(executable)}}}
    launchfile = account/"Library/LaunchAgents"/(LABEL+".plist")
    backup = ROOT/".artifacts/continuity-20261025/shared-service-before"
    backup.mkdir(parents=True,exist_ok=True)
    for p in [home/"host.json",launchfile]:
        if p.exists() and not (backup/p.name).exists(): shutil.copy2(p,backup/p.name)
    service = f"gui/{os.getuid()}/{LABEL}"
    prior = subprocess.run(["launchctl","print",service],capture_output=True)
    if prior.returncode == 0:
        subprocess.run(["launchctl","bootout",service],check=True)
        deadline = time.monotonic()+55
        while subprocess.run(["launchctl","print",service],capture_output=True).returncode == 0:
            if time.monotonic() >= deadline:
                raise ValueError('previous_owner_still_running')
            time.sleep(.25)
    # Atomic, private host configuration; no RPC can alter it.
    temp = home/"host.pending"
    temp.write_text(json.dumps(config));temp.chmod(0o600);temp.replace(home/"host.json")
    spec = {"Label":LABEL,"ProgramArguments":[str(ROOT/"data/embodied/runtime/bin/python"),"-B","-m","homeplane.shared_laya_service"],
            "WorkingDirectory":str(release),"RunAtLoad":True,"KeepAlive":True,
            "ThrottleInterval":15,"ExitTimeOut":50,
            "EnvironmentVariables":{"PYTHONPATH":str(release),"HF_HUB_OFFLINE":"1","HF_HUB_DISABLE_PROGRESS_BARS":"1","TOKENIZERS_PARALLELISM":"false"},
            "StandardOutPath":"/dev/null","StandardErrorPath":"/dev/null"}
    launchfile.parent.mkdir(parents=True,exist_ok=True)
    launchfile.write_bytes(plistlib.dumps(spec));launchfile.chmod(0o600)
    subprocess.run(["launchctl","bootstrap",f"gui/{os.getuid()}",str(launchfile)],check=True)
    receipt.update(installed=True,release=str(release),model_sha256=pins)
    (ROOT/".artifacts/continuity-20261025/shared-install.json").write_text(json.dumps(receipt,indent=2))
    print(json.dumps({"installed":True,"label":LABEL,"version":"0.5.0","source_files":len(hashes)}))


if __name__ == "__main__": main()
