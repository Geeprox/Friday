"""Install the verified KTV audio companion, then KTV, through the selected TCL install UI.

Dry-run verifies both local builds without contacting a device. Installation reports are
transport observations; they do not establish runtime, microphone, or latency acceptance.
"""
from __future__ import annotations

import argparse
import hashlib
import http.server
import ipaddress
import json
import math
import os
import secrets
import shutil
import socket
import sys
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from build_ktv_audio_guard import (
    CONTROL,
    NS,
    digest,
    inspect_apk,
    java_environment,
    manifest_identity,
    source_snapshot,
    validate_companion_manifest,
)
from build_ktv_audio_guard import PACKAGE as AUDIO_PACKAGE

ROOT = Path(__file__).resolve().parents[1]
PACKAGE, ACTIVITY = "local.friday.ktv", "local.friday.ktv.MainActivity"


@dataclass(frozen=True)
class VerifiedBuild:
    role: str
    identity: dict
    receipt: dict
    apk: Path
    body: bytes
    certificate_sha256: str

    def summary(self) -> dict:
        return {**self.identity, "sha256": self.receipt["sha256"],
                "certificate_sha256": self.certificate_sha256, "role": self.role}


def verify_build(role: str, output: Path, sdk: Path, environment: dict, root: Path = ROOT) -> VerifiedBuild:
    if role == "audio":
        source, package, prefix, receipt_name = (root / "native/KTVTV/audio-guard", AUDIO_PACKAGE,
                                                 "friday-ktv-audio-", "audio-build-result.json")
        identity = validate_companion_manifest(source)
    elif role == "main":
        source, package, prefix, receipt_name = (root / "native/KTVTV/android", PACKAGE,
                                                 "friday-ktv-", "build-result.json")
        identity = manifest_identity(source, package)
        tree = ET.parse(source / "AndroidManifest.xml").getroot()
        queries = {node.get(NS + "name") for node in tree.findall("queries/package")}
        if (set(identity["permissions"]) != {"android.permission.INTERNET", "android.permission.RECORD_AUDIO", "android.permission.MODIFY_AUDIO_SETTINGS", CONTROL}
                or AUDIO_PACKAGE not in queries or identity["min_api"] != 26 or identity["target_api"] != 35):
            raise ValueError("main_audio_companion_contract_required")
    else:
        raise ValueError("unknown_build_role")
    receipt_path = output / receipt_name
    if receipt_path.resolve() != receipt_path or not receipt_path.is_file():
        raise ValueError("reviewed_workspace_receipt_required")
    receipt = json.loads(receipt_path.read_text())
    if (receipt.get("package") != package or receipt.get("version") != identity["version"]
            or receipt.get("signature_verified") is not True or receipt.get("permissions_verified") is not True):
        raise ValueError("verified_build_receipt_required")
    if role == "audio":
        if (receipt.get("schema") != 1 or any(receipt.get(k) != v for k, v in identity.items())
                or receipt.get("household_provisioned") is not False
                or receipt.get("same_signing_store_as_main") is not True):
            raise ValueError("reviewed_audio_receipt_required")
        if receipt.get("builder_sha256") != {"scripts/build_ktv_audio_guard.py": digest(root / "scripts/build_ktv_audio_guard.py")}:
            raise ValueError("audio_builder_drift")
    # Exact set equality also catches a new source file omitted by an older receipt.
    shared_sources = source_snapshot(root / "native/KTVTV/strict-cache", root)
    if receipt.get("source_sha256") != {**source_snapshot(source, root), **shared_sources}:
        raise ValueError("build_source_drift")
    dependencies = receipt.get("dependency_sha256", {})
    if role == "main" and receipt.get("native_audio") is True:
        dependency = "native/KTVTV/dependencies/oboe.json"
        if dependencies != {dependency: digest(root / dependency)}:
            raise ValueError("build_dependency_drift")
    elif dependencies:
        raise ValueError("unexpected_build_dependency")
    apk = output / (prefix + identity["version"] + ".apk")
    if not apk.is_file() or apk.resolve() != apk:
        raise ValueError("reviewed_workspace_apk_required")
    body = apk.read_bytes()
    if hashlib.sha256(body).hexdigest() != receipt.get("sha256") or len(body) != receipt.get("bytes"):
        raise ValueError("verified_apk_required")
    cert = inspect_apk(apk, identity, sdk, environment)
    if role == "audio" and receipt.get("certificate_sha256") != cert:
        raise ValueError("audio_certificate_receipt_mismatch")
    # Ensure the immutable bytes served below are the bytes just inspected by SDK tools.
    if (digest(apk) != receipt["sha256"]
            or receipt.get("source_sha256") != {**source_snapshot(source, root), **source_snapshot(root / "native/KTVTV/strict-cache", root)}):
        raise ValueError("build_changed_during_verification")
    return VerifiedBuild(role, identity, receipt, apk, body, cert)


def verify_builds(output: Path, sdk: Path, java: Path, root: Path = ROOT) -> list[VerifiedBuild]:
    if output.resolve() != output:
        raise ValueError("canonical_workspace_output_required")
    environment = java_environment(java)
    builds = [verify_build(role, output, sdk, environment, root) for role in ("audio", "main")]
    if len({build.certificate_sha256 for build in builds}) != 1:
        raise ValueError("audio_and_main_signatures_must_match")
    return builds


def review_install_prompt(session, build: VerifiedBuild, output: Path, deadline: float) -> bool:
    session.sock.settimeout(5)
    # TCL's permission panel slides in from the right. A screenshot immediately
    # after confirm_pending can contain only its cropped, still-moving edge.
    # Wait for that transition before asking the operator to review its buttons.
    if deadline - time.monotonic() < 1:
        return False
    time.sleep(1)
    screen = session.screenshot()
    if not screen:
        return False
    prompt = output / ("install-" + build.role + "-prompt.png")
    shutil.copyfile(screen, prompt)
    prompt.chmod(0o600)
    marker = output / ("confirm-once-" + build.role + "-" + secrets.token_hex(8))
    print(json.dumps({"package": build.identity["package"], "prompt_image": str(prompt),
                      "confirm_once_file": str(marker)}), flush=True)
    review_deadline, beat = min(deadline, time.monotonic() + 60), time.monotonic()
    try:
        while time.monotonic() < review_deadline and not marker.exists():
            time.sleep(0.2)
            if time.monotonic() - beat > 4:
                session.send("150>>")
                beat = time.monotonic()
        if not marker.is_file() or marker.resolve() != marker:
            return False
        marker.unlink()
        # Only after review of this package's actual two-button install prompt.
        for key in (13, 14, 15):
            session.send("149>>" + str(key))
            time.sleep(0.3)
        return True
    finally:
        if marker.exists() and marker.resolve() == marker:
            marker.unlink()
        session.sock.settimeout(1)


def install_build(build: VerifiedBuild, host: str, output: Path, session_factory,
                  review_prompt: bool) -> dict:
    package, body = build.identity["package"], build.body
    request_path = "/" + secrets.token_urlsafe(24) + "/" + build.apk.name
    served = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != request_path or self.client_address[0] != host:
                self.send_error(404)
                return
            self.connection.settimeout(15)
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.android.package-archive")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            served.set()

        def log_message(self, *unused):
            pass

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
        route.connect((host, 6553))
        local = route.getsockname()[0]
    server = http.server.ThreadingHTTPServer((local, 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    session, outcome, installed, last_status = None, "timeout", False, "none"
    result = {"role": build.role, "package": package, "apk_sha256": build.receipt["sha256"],
              "runtime_verified": False, "launch_requested": False}
    try:
        session = session_factory()
        # Complete the normal app-list handshake before the installation dialog.
        # Some firmware stops answering GET while its confirmation modal is up.
        session.send("223>>GET")
        session.until("223>>GET>>")
        url = "http://" + local + ":" + str(server.server_port) + request_path
        label = "Friday KTV Audio" if build.role == "audio" else "Friday KTV"
        command = ">>".join(["223", "INSTALL", package, label, url, "2", str(build.identity["version_code"]),
                             str(max(1, math.ceil(len(body) / 1048576))), "", "", "",
                             hashlib.md5(url.encode()).hexdigest()])
        session.send(command)
        print(json.dumps({"install_prompt_requested": True, "package": package}), flush=True)
        session.sock.settimeout(1)
        deadline, beat, confirmed = time.monotonic() + 180, time.monotonic(), False
        while time.monotonic() < deadline:
            try:
                fields = session.receive().split(">>")
            except socket.timeout:
                fields = []
            if len(fields) >= 5 and fields[0] == "223" and fields[2] == package:
                last_status = fields[4] if fields[4] in {"confirm_pending", "denied", "installing", "installed", "0", "1", "2", "3", "4", "5"} else "other"
                print(json.dumps({"package": package,
                                  "operation": fields[1] if fields[1] in {"INSTALL", "PROGRESS"} else "other",
                                  "status": last_status}), flush=True)
                if review_prompt and not confirmed and fields[1] == "INSTALL" and last_status == "confirm_pending":
                    confirmed = review_install_prompt(session, build, output, deadline)
                    if not confirmed:
                        outcome = "prompt_review_not_completed"
                        break
                if fields[1] == "INSTALL" and last_status == "denied":
                    outcome = "denied"
                    break
                if fields[1] == "PROGRESS" and last_status == "2":
                    outcome = "failed"
                    break
                if (fields[1] == "PROGRESS" and last_status == "5") or (fields[1] == "INSTALL" and last_status == "installed"):
                    installed, outcome = True, "installed_reported"
                    break
            if time.monotonic() - beat > 4:
                session.send("150>>")
                beat = time.monotonic()
    except Exception:
        outcome = "transport_error"
        raise
    finally:
        if session is not None:
            session.sock.close()
        server.shutdown()
        server.server_close()
        result.update({"outcome": outcome, "apk_served": served.is_set(),
                       "installed_reported": installed, "last_status": last_status})
        (output / ("install-" + build.role + "-result.json")).write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result), flush=True)
    return result


def install_pair(builds: list[VerifiedBuild], host: str, output: Path, session_factory,
                 review_prompt: bool, no_launch: bool) -> dict:
    if [build.role for build in builds] != ["audio", "main"]:
        raise ValueError("audio_then_main_required")
    result = {"packages": [], "outcome": "not_started", "installed_reported": False,
              "main_launch_requested": False, "runtime_verified": False,
              "audio_state_verified": False, "physical_latency_verified": False,
              "microphone_command_sent": False}
    try:
        for build in builds:
            step = install_build(build, host, output, session_factory, review_prompt)
            result["packages"].append(step)
            if not step["installed_reported"]:
                result["outcome"] = build.role + "_" + step["outcome"]
                break
        else:
            result.update({"outcome": "both_installed_reported", "installed_reported": True})
            if not no_launch:
                session = session_factory()
                try:
                    intent = {"action": "android.intent.action.MAIN", "category": "android.intent.category.LAUNCHER",
                              "component": {"pkg": PACKAGE, "cls": ACTIVITY}}
                    session.send("263>>1>>" + json.dumps(intent, separators=(",", ":")))
                    result["main_launch_requested"] = True
                finally:
                    session.sock.close()
    except Exception:
        result["outcome"] = "transport_error"
        raise
    finally:
        (output / "install-result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result), flush=True)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "data/embodied/private/config.json")
    parser.add_argument("--sdk", type=Path, default=ROOT / ".artifacts/embodied-toolchain/android")
    parser.add_argument("--java", type=Path, default=ROOT / ".artifacts/embodied-toolchain/jdk")
    parser.add_argument("--dry-run", action="store_true", help="Verify both APKs, receipts and signers without device access")
    parser.add_argument("--review-prompt", action="store_true", help="Wait for local one-time confirmation after each install-prompt screenshot")
    parser.add_argument("--no-launch", action="store_true", help="Install both packages without opening the main app")
    args = parser.parse_args()
    os.umask(0o077)
    output = ROOT / ".artifacts/ktv-tv"
    builds = verify_builds(output, args.sdk, args.java, ROOT)
    if args.dry_run:
        print(json.dumps({"verified_build": True, "packages": [build.summary() for build in builds],
                          "install_order": [build.identity["package"] for build in builds],
                          "same_signer_verified": True, "device_contacted": False,
                          "main_launch_planned": not args.no_launch}, indent=2))
        return 0
    config = args.config.absolute()
    if config.resolve() != config or not config.is_file() or config.stat().st_mode & 0o077:
        raise ValueError("private_selected_tv_config_required")
    address = ipaddress.ip_address(json.loads(config.read_text())["tv_host"])
    if address.version != 4 or not address.is_private or address.is_loopback or address.is_unspecified or address.is_multicast:
        raise ValueError("selected_private_tv_ipv4_required")
    host = str(address)
    os.environ["TV_DIAGNOSTIC_HOST"] = host
    sys.path.insert(0, str(ROOT / "scripts/embodied"))
    from tcl_diagnostic_session import Session

    result = install_pair(builds, host, output, Session, args.review_prompt, args.no_launch)
    return 0 if result["installed_reported"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
