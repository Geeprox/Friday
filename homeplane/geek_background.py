"""Bounded local client for the read-only Geek owner. No direct fallback."""
from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

from .geek_background_contracts import BackgroundInstallation, BackgroundRequest
from .geek_daemon import MAX_RESPONSE, SOCKET_NAME, private_file
from .geek_inventory import GeekInventoryView
from .geek_session import GeekSessionView
from .geek_setup import GeekSetup

MARKER_NAME = "geek-background-installed.json"


def configured(directory):
    marker = Path(directory) / MARKER_NAME
    # Even a corrupt marker must never cause a second direct owner to launch.
    return marker.exists() or marker.is_symlink()


def enabled(directory):
    if not configured(directory):
        return False
    try:
        return BackgroundInstallation.model_validate_json(private_file(Path(directory) / MARKER_NAME)).use_for_connections
    except (OSError, ValueError):
        return True  # Corrupt configuration never admits a competing owner.


def last_attempt(directory):
    try:
        attempt = BackgroundInstallation.model_validate_json(private_file(Path(directory) / MARKER_NAME)).last_attempt
        return attempt.model_dump(exclude_none=True) if attempt else None
    except (OSError, ValueError):
        return None


class GeekBackground:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.setup = GeekSetup(self.directory)
        self.report = self.unavailable()

    def unavailable(self):
        return GeekSessionView(status="background_unavailable", session_live=False,
            credential_available=self.setup.available(), inventory_stale=True,
            connection_host="background_service", background_service_live=False).model_dump(exclude_none=True)

    async def request(self, operation, url=None):
        BackgroundInstallation.model_validate_json(private_file(self.directory / MARKER_NAME))
        path = self.directory / SOCKET_NAME
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError("private_socket_required")
        info = path.stat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("private_socket_required")
        request = BackgroundRequest(operation=operation, url=url)
        writer = None
        try:
            async with asyncio.timeout(4 if operation in {"status", "inventory"} else 14):
                reader, writer = await asyncio.open_unix_connection(str(path), limit=MAX_RESPONSE)
                writer.write(request.model_dump_json().encode() + b"\n")
                await writer.drain()
                line = await reader.readline()
                if len(line) > MAX_RESPONSE or not line.endswith(b"\n"):
                    raise ValueError("background_response_budget")
                model = GeekInventoryView if operation == "inventory" else GeekSessionView
                result = model.model_validate_json(line)
                report = result.session if operation == "inventory" else result
                if report.connection_host != "background_service" or not report.background_service_live:
                    raise ValueError("background_owner_required")
                self.report = report.model_dump(exclude_none=True)
                return result
        finally:
            if writer:
                writer.close()
                try:
                    await asyncio.wait_for(writer.wait_closed(), 1)
                except (OSError, TimeoutError):
                    pass

    async def refresh(self):
        try:
            await self.request("status")
        except (OSError, ValueError, TimeoutError):
            self.report = self.unavailable()
        return self.status()

    def status(self):
        return {**self.report, "background_last_attempt": last_attempt(self.directory)}

    def live(self):
        return self.report.get("session_live", False)

    async def connect(self, url):
        try:
            await self.request("connect", url)
        except (OSError, ValueError, TimeoutError):
            self.report = self.unavailable()
        return self.status()

    async def close(self):
        # Explicit stop, endpoint edit and credential edit stop persisted intent.
        # An unreachable owner cannot be reported as a successful stop.
        try:
            await self.request("disconnect")
        except (OSError, ValueError, TimeoutError):
            self.report = self.unavailable()
            raise ValueError("background_stop_unverified") from None

    async def release(self):
        # Console lifecycle never disconnects the separately supervised owner.
        pass

    async def inventory(self):
        try:
            result = await self.request("inventory")
            if result.status == "ready" and (result.session.status != "online" or result.fingerprint != result.session.fingerprint):
                return GeekInventoryView(session=self.status(), status="stale")
            return result
        except (OSError, ValueError, TimeoutError):
            self.report = self.unavailable()
            return GeekInventoryView(session=self.status(), status="unavailable")
