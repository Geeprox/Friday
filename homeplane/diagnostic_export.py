"""Explicit CLI download from the running loopback Console; no adapter startup."""

from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .diagnostics import MAX_BYTES, DiagnosticsPolicy, DiagnosticsReport, bundle


def console_origin(url):
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or not parsed.port
            or not 1 <= parsed.port <= 65535
        ):
            raise ValueError("local_console_required")
        return f"http://127.0.0.1:{parsed.port}"
    except (TypeError, ValueError):
        raise ValueError("local_console_required") from None


def validate_download(data):
    if len(data) > MAX_BYTES:
        raise ValueError("diagnostics_budget_exceeded")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            files = archive.infolist()
            if (
                len(files) != 2
                or {f.filename for f in files} != {"report.json", "manifest.json"}
                or any(f.compress_type != zipfile.ZIP_STORED for f in files)
                or any(f.file_size > 32768 for f in files)
            ):
                raise ValueError("invalid_diagnostics_bundle")
            body = archive.read("report.json")
            manifest = json.loads(archive.read("manifest.json"))
            expected = {
                "schema_version": 1,
                "redacted": True,
                "files": [
                    {"name": "report.json", "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}
                ],
            }
            if manifest != expected:
                raise ValueError("invalid_diagnostics_bundle")
            report = DiagnosticsReport.model_validate_json(body)
            if report.policy != DiagnosticsPolicy():
                raise ValueError("invalid_diagnostics_bundle")
            # Rebuild from typed fields; arbitrary ZIP comments/extra metadata are not retained.
            return bundle(report)
    except (OSError, ValueError, TypeError, KeyError, zipfile.BadZipFile, RuntimeError):
        raise ValueError("invalid_diagnostics_bundle") from None


def download(url, *, transport=None):
    origin = console_origin(url)
    with httpx.Client(transport=transport, timeout=4, follow_redirects=False, trust_env=False) as client:
        with client.stream(
            "POST", origin + "/api/diagnostics/export", json={"redacted": True}, headers={"Origin": origin}
        ) as response:
            if response.status_code != 200 or response.headers.get("content-type") != "application/zip":
                raise ValueError("diagnostics_unavailable")
            output = bytearray()
            for chunk in response.iter_bytes(chunk_size=8192):
                if len(output) + len(chunk) > MAX_BYTES:
                    raise ValueError("diagnostics_budget_exceeded")
                output.extend(chunk)
    return validate_download(bytes(output))


def save(data, path):
    path = Path(path).expanduser().absolute()
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError("local_regular_output_required")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    owned = os.fstat(fd)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
    except BaseException:
        try:
            current = path.lstat()
            if (current.st_dev, current.st_ino) == (owned.st_dev, owned.st_ino):
                path.unlink()
        except OSError:
            pass
        raise
    return {
        "status": "saved",
        "path": str(path),
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "redacted": True,
        "automatic_upload": False,
        "retention": "user_managed",
    }
