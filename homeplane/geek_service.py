"""Install exactly one user LaunchAgent for the read-only adapter owner."""
from __future__ import annotations

import asyncio
import os
import platform
import plistlib
import shutil
from datetime import UTC, datetime
from pathlib import Path

from .geek_background import MARKER_NAME, GeekBackground
from .geek_background_contracts import BackgroundInstallation
from .geek_daemon import atomic_private, private_file
from .geek_native import BUNDLE, BUNDLE_ID, build_helper
from .geek_session import BackgroundAttempt
from .geek_setup import ROOT
from .settings import run_bounded

LABEL = "local.friday.geek-readonly"
PERMISSION_POLLS = 60


def legacy_definition(directory, node):
    return {"Label": LABEL,
        "ProgramArguments": [str(ROOT / ".venv/bin/python"), "-m", "homeplane.geek_daemon", "--directory", str(directory)],
        "WorkingDirectory": str(ROOT), "RunAtLoad": True, "KeepAlive": True,
        "ThrottleInterval": 30, "Umask": 0o077,
        "EnvironmentVariables": {"PATH": str(Path(node).parent) + ":/usr/bin:/bin:/usr/sbin:/sbin"},
        "StandardOutPath": "/dev/null", "StandardErrorPath": "/dev/null"}


def definition(directory, node):
    body = legacy_definition(directory, node)
    body["ProgramArguments"] = [str(BUNDLE / "Contents/MacOS/FridayGeekConnector")]
    body["AssociatedBundleIdentifiers"] = [BUNDLE_ID]
    return body


async def install(directory, *, home=None, runner=run_bounded, node=None):
    if platform.system() != "Darwin":
        raise ValueError("macos_required")
    directory = Path(directory).absolute()
    marker = directory / MARKER_NAME
    installation = BackgroundInstallation.model_validate_json(private_file(marker)) if marker.exists() or marker.is_symlink() else BackgroundInstallation()
    node = node or shutil.which("node")
    if not node or not (ROOT / ".venv/bin/python").is_file():
        raise ValueError("runtime_required")
    agents = (Path(home) if home is not None else Path.home()) / "Library/LaunchAgents"
    if any(p.is_symlink() for p in (agents, *agents.parents)):
        raise ValueError("private_agent_path_required")
    agents.mkdir(parents=True, exist_ok=True)
    if agents.stat().st_uid != os.getuid():
        raise ValueError("owned_agent_directory_required")
    path = agents / (LABEL + ".plist")
    body = definition(directory, node)
    previous = None
    if path.exists() or path.is_symlink():
        previous = plistlib.loads(private_file(path, 16384))
        if previous not in [body, legacy_definition(directory, node)]:
            raise ValueError("existing_service_conflict")
    # Compile/verify before touching the active job. No signed identity spoofing
    # or root/system daemon is used to bypass Local Network permission.
    await build_helper(directory, node)
    target = f"gui/{os.getuid()}/{LABEL}"
    if previous is not None and previous != body:
        report = await GeekBackground(directory).refresh()
        if not report["background_service_live"] or report["session_live"] or report["keep_connected"]:
            raise ValueError("service_update_requires_stop")
        code, _ = await runner(["/bin/launchctl", "bootout", target], timeout=10)
        if code != 0:
            raise ValueError("service_stop_unverified")
        # Wait for this owned socket to be released before admitting a new owner.
        for _ in range(50):
            if not (directory / "geek-background.sock").exists():
                break
            await asyncio.sleep(.1)
    if previous != body:
        # atomic_private does not chmod shared Library/LaunchAgents directory.
        from tempfile import mkstemp
        fd, name = mkstemp(prefix=".friday-geek-", dir=agents)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                plistlib.dump(body, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
    loaded, _ = await runner(["/bin/launchctl", "print", target], timeout=5)
    if loaded != 0:
        code, _ = await runner(["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)], timeout=10)
        if code != 0:
            raise ValueError("service_bootstrap_failed")
    # Mark installed only after a service-target check. No connection or PIN is
    # restored unless an earlier explicit readonly intent exists in the daemon.
    code, _ = await runner(["/bin/launchctl", "print", target], timeout=5)
    if code != 0:
        raise ValueError("service_start_unverified")
    atomic_private(marker, installation.model_dump_json() + "\n")
    client = GeekBackground(directory)
    for _ in range(20):
        report = await client.refresh()
        if report["background_service_live"]:
            return report
        await asyncio.sleep(.1)
    raise ValueError("service_socket_unverified")


async def enable(directory):
    """Admit the background owner only after one complete readonly inventory."""
    from .settings import SettingsService
    client = GeekBackground(directory)
    report = await client.connect(SettingsService(Path(directory) / "settings.json").config.geek_url)
    for _ in range(PERMISSION_POLLS):
        network_wait = report.get("failure_phase") == "websocket_connect" and report.get("failure_code") == "network_unreachable"
        if report["status"] not in {"starting", "reconnecting"} and not network_wait:
            break
        await asyncio.sleep(.25)
        report = await client.refresh()
    marker = Path(directory) / MARKER_NAME
    installation = BackgroundInstallation.model_validate_json(private_file(marker))
    connected = report["status"] == "online" and report["session_live"] and not report["inventory_stale"]
    installation.last_attempt = BackgroundAttempt(checked_at=datetime.now(UTC).isoformat(),
        outcome="connected" if connected else "failed",
        **{key: report[key] for key in ("failure_phase", "failure_code", "network_errno") if key in report})
    if connected:
        installation.use_for_connections = True
    atomic_private(marker, installation.model_dump_json() + "\n")
    if connected:
        return {"enabled": True, "background": client.status()}
    # A failed preparation cannot switch the Console away from its usable
    # owner, or leave competing automatic retries in the background.
    await client.close()
    return {"enabled": False, "background": client.status()}


async def remove(directory, *, home=None, runner=run_bounded):
    directory = Path(directory).absolute()
    path = (Path(home) if home is not None else Path.home()) / "Library/LaunchAgents" / (LABEL + ".plist")
    if not path.exists():
        raise ValueError("installed_service_required")
    body = plistlib.loads(private_file(path, 16384))
    if body.get("Label") != LABEL or body.get("ProgramArguments") not in [definition(directory, "/unused")['ProgramArguments'], legacy_definition(directory, "/unused")['ProgramArguments']]:
        raise ValueError("existing_service_conflict")
    client = GeekBackground(directory)
    await client.close()  # Clear explicit intent before disabling process supervision.
    code, _ = await runner(["/bin/launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], timeout=10)
    if code != 0:
        raise ValueError("service_stop_unverified")
    path.unlink()
    (directory / MARKER_NAME).unlink(missing_ok=True)
    return {"status": "removed", "writes_attempted": False}
