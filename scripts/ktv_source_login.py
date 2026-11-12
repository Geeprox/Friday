"""Build and open the one-time first-party KTV source authorization window."""
import argparse
import json
import plistlib
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-only", action="store_true")
    args = parser.parse_args()
    output = ROOT / ".artifacts/ktv-source/OfficialLogin.app"
    contents = output / "Contents"
    binary = contents / "MacOS/OfficialLogin"
    binary.parent.mkdir(parents=True, exist_ok=True)
    source = ROOT / "integrations/ktv_source/OfficialLogin.swift"
    result = subprocess.run(["/usr/bin/swiftc", "-module-cache-path", str(ROOT / ".artifacts/ktv-source/swift-cache"),
        "-framework", "AppKit", "-framework", "WebKit", str(source), "-o", str(binary)],
        capture_output=True, timeout=120)
    if result.returncode:
        (contents.parent.parent / "login-build-error.txt").write_bytes(result.stderr[-16000:])
        raise RuntimeError("login_helper_build_failed")
    (contents / "Info.plist").write_bytes(plistlib.dumps({"CFBundleExecutable": "OfficialLogin",
        "CFBundleIdentifier": "local.friday.ktv.source-login", "CFBundleName": "Friday KTV 授权",
        "CFBundlePackageType": "APPL", "LSMinimumSystemVersion": "13.0", "NSPrincipalClass": "NSApplication"}))
    subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(output)], check=True, capture_output=True)
    if args.build_only:
        print(json.dumps({"built": True, "launched": False, "credentials_read": False}))
        return
    config = json.loads((ROOT / "data/ktv/config.json").read_text())
    destination = Path(config["root"]) / "source-account/session.json"
    subprocess.run(["/usr/bin/open", "-n", str(output), "--args", str(destination)], check=True)
    print(json.dumps({"built": True, "launched": True, "website": "https://music.163.com/", "credentials_printed": False}))


if __name__ == "__main__":
    main()
