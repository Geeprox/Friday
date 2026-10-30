"""Private persistent connection settings and explicitly requested read-only probes.

HTTP host/origin/client-loopback enforcement belongs to the API boundary. This service
never mounts shares, starts cameras, runs models or accepts arbitrary commands.
"""
from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SettingsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    ha_url: str | None = Field(default=None, max_length=512)
    ha_instance: str | None = Field(default=None, max_length=96)
    nas_url: str | None = Field(default=None, max_length=1024)
    nas_path: str | None = Field(default=None, max_length=1024)
    camera_service_url: str | None = Field(default=None, max_length=512)
    camera_id: str | None = Field(default=None, max_length=128)
    camera_media_source: Literal["miloco_service", "miot_sdk"] = "miloco_service"
    camera_recognition_enabled: Literal[False] = False
    geek_url: str | None = Field(default=None, max_length=512)
    arl_home: str | None = Field(default=None, max_length=90)

    @field_validator("camera_recognition_enabled", mode="before")
    @classmethod
    def recognition_stays_off(cls, value):
        if value is not False:
            raise ValueError("recognition_activation_unavailable")
        return value

    @field_validator("ha_url", "camera_service_url", "geek_url", "nas_url")
    @classmethod
    def safe_url(cls, value, info):
        if value is None or value == "":
            return None
        parsed = urlsplit(value)
        scheme = {"smb"} if info.field_name == "nas_url" else {"http", "https"}
        if (parsed.scheme not in scheme or not parsed.hostname or parsed.username or parsed.password or
                parsed.query or parsed.fragment or any(ord(c) < 32 for c in value) or "\\" in value):
            raise ValueError("invalid_credential_free_url")
        try:
            parsed.port
        except ValueError:
            raise ValueError("invalid_port") from None
        if any(p in {".", ".."} for p in unquote(parsed.path).split("/")):
            raise ValueError("path_traversal")
        if info.field_name == "nas_url" and (parsed.port or len(parsed.path.strip("/")) == 0):
            raise ValueError("smb_share_required")
        if info.field_name == "camera_service_url":
            # Credential-bearing status requests must never target a LAN/remote host.
            try:
                loopback = ipaddress.ip_address(parsed.hostname).is_loopback
            except ValueError:
                loopback = parsed.hostname == "localhost"
            if not loopback or parsed.path not in {"", "/"}:
                raise ValueError("camera_service_must_be_loopback_origin")
        return value.rstrip("/")

    @field_validator("nas_path", "arl_home")
    @classmethod
    def safe_path(cls, value):
        if not value:
            return None
        if (not Path(value).is_absolute() or ".." in Path(value).parts or
                any(ord(c) < 32 for c in value)):
            raise ValueError("absolute_nas_path_required")
        if value != str(Path(value)):
            raise ValueError("canonical_path_required")
        return value

    @field_validator("camera_id", "ha_instance")
    @classmethod
    def safe_identifier(cls, value):
        if value is not None and any(ord(c) < 32 for c in value):
            raise ValueError("invalid_identifier")
        return value or None

    @model_validator(mode="after")
    def one_nas_location(self):
        if self.nas_url and self.nas_path:
            raise ValueError("choose_nas_url_or_path")
        return self


class PublicSettings(BaseModel):
    schema_version: int = 1
    config: dict
    checks: dict
    credentials: dict


def resolve_smb_path(url: str, mounts: str) -> Path | None:
    """Resolve only an already mounted exact server/share; never infer /Volumes names."""
    requested = urlsplit(url)
    parts = unquote(requested.path).strip("/").split("/")
    if not parts or any(p in {".", ".."} for p in parts):
        raise ValueError("invalid_smb_path")
    for line in mounts.splitlines():
        match = re.fullmatch(r"(//.+) on (/.+) \(smbfs(?:,.*)?\)", line)
        if not match:
            continue
        source = urlsplit("smb:" + match.group(1))
        # Finder can mount a subdirectory of a share directly. Preserve the
        # exact server and path prefix rather than appending that prefix twice.
        mounted_parts = unquote(source.path).strip("/").split("/")
        if (source.hostname and source.hostname.lower().rstrip('.') == requested.hostname.lower().rstrip('.')
                and mounted_parts and parts[:len(mounted_parts)] == mounted_parts):
            return Path(match.group(2)).joinpath(*parts[len(mounted_parts):])
    return None


async def run_bounded(command: list[str], timeout: float = 4) -> tuple[int, bytes]:
    process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.DEVNULL,
                                                 env={k: os.environ[k] for k in ("PATH", "LANG", "TMPDIR") if k in os.environ})
    try:
        data = await asyncio.wait_for(process.stdout.read(65537), timeout)
        if len(data) > 65536:
            raise ValueError("probe_output_limit")
        await asyncio.wait_for(process.wait(), timeout)
        return process.returncode, data
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


def scan_directory(path: str) -> dict:
    root = Path(path)
    if root.is_symlink() or any(p.is_symlink() for p in root.parents):
        return {"status": "symlink_path_rejected", "mounted": True, "readable": False}
    count = folders = 0
    try:
        with os.scandir(root) as entries:
            for item in entries:
                if count >= 64:
                    return {"status": "readable", "mounted": True, "readable": True,
                            "directory_count": folders, "entry_count": count, "truncated": True}
                count += 1
                folders += int(item.is_dir(follow_symlinks=False))
        return {"status": "readable", "mounted": True, "readable": True,
                "directory_count": folders, "entry_count": count, "truncated": False}
    except (PermissionError, FileNotFoundError, OSError):
        return {"status": "path_unavailable", "mounted": True, "readable": False}


class SettingsService:
    @property
    def nas_connection_state_path(self) -> Path:
        # Explicit direct connection configuration is separate from legacy mount settings.
        return self.path.with_name("nas-connection.json")

    async def resolve_nas_credential(self, credential_ref: str):
        """Resolve an opaque NAS ref through an explicitly supplied private owner.

        This seam never searches files or reuses HA/Geek/camera credentials. The
        private provider must separately support this exact NAS read-only purpose.
        Its ephemeral result is consumed only by the existing NAS manager.
        """
        if (self._nas_credential_resolver is None or not isinstance(credential_ref, str)
                or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", credential_ref) is None):
            return None
        from .nas_connection.contracts import NASCredentials
        try:
            value = await self._nas_credential_resolver(
                credential_ref, purpose="friday.nas.read-only.v1"
            )
            if (not isinstance(value, NASCredentials)
                    or not isinstance(value.username, str) or not isinstance(value.password, str)):
                return None
            value.payload()  # Validate in memory; never persist/log/export private values.
            return value
        except Exception:
            # A private provider may include sensitive values in its exception text.
            # The manager receives only unresolved-credential evidence, never that text.
            return None

    def __init__(self, path: Path | str = "data/homeplane/settings.json", *,
                 token_path: Path | str = "data/miloco/config.json", transport=None,
                 nas_credential_resolver=None):
        if nas_credential_resolver is not None and not callable(nas_credential_resolver):
            raise ValueError("nas_private_resolver_required")
        self._nas_credential_resolver = nas_credential_resolver
        self.path = Path(path)
        self.token_path = Path(token_path)
        self.transport = transport
        self.config = SettingsConfig()
        self.checks = {}
        self.revision = 0
        if self.path.exists():
            if self.path.is_symlink() or self.path.stat().st_size > 16384:
                raise ValueError("invalid_settings_file")
            stored = json.loads(self.path.read_text())
            self.config = SettingsConfig.model_validate(stored.get("config", {}))
            # Old health probes are stale after process startup. Do not present cached green.
            self.checks = {}
            os.chmod(self.path, 0o600)

    def get_public(self) -> dict:
        config = self.config.model_dump()
        camera_id = config.pop("camera_id")
        config["camera_id_configured"] = bool(camera_id)
        config["camera_id_masked"] = "••••" + camera_id[-4:] if camera_id else None
        checks = {}
        now = datetime.now(timezone.utc)
        for name, report in self.checks.items():
            try:
                recent = (now - datetime.fromisoformat(report["checked_at"])).total_seconds() < 60
            except (KeyError, ValueError, TypeError):
                recent = False
            checks[name] = report.copy() if recent else {"status": "check_expired", "checked_at": report.get("checked_at"),
                                                        "readable": False, "stream_verified": False}
        return PublicSettings(config=config, checks=checks, credentials={
            "ha": "managed_separately", "geek": "managed_separately",
            "camera": "local_service_token_reference", "secret_values_returned": False,
            "nas": "opaque_reference_only" if self._nas_credential_resolver is not None
                   else "private_resolver_not_configured",
        }).model_dump()

    def _persist(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink() or any(p.is_symlink() for p in self.path.parents):
            raise ValueError("settings_symlink_rejected")
        fd, name = tempfile.mkstemp(prefix=".settings-", dir=self.path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump({"schema_version": 1, "config": self.config.model_dump()}, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def update(self, config: SettingsConfig | dict) -> dict:
        # PATCH semantics preserve masked/omitted IDs; explicit null clears values.
        patch = config.model_dump(exclude_unset=True) if isinstance(config, SettingsConfig) else config
        validated = SettingsConfig.model_validate({**self.config.model_dump(), **patch})
        previous = self.config
        self.config = validated
        try:
            self._persist()
        except Exception:
            self.config = previous
            raise
        self.revision += 1
        self.checks.clear()
        return self.get_public()

    def _record(self, name, report, revision):
        if revision != self.revision:
            return {"status": "discarded_configuration_changed"}
        report["checked_at"] = datetime.now(timezone.utc).isoformat()
        self.checks[name] = report
        return report

    async def probe_nas(self) -> dict:
        revision = self.revision
        url, path = self.config.nas_url, self.config.nas_path
        if not (url or path):
            return self._record("nas", {"status": "not_configured", "readable": False}, revision)
        try:
            if url:
                code, output = await run_bounded(["/sbin/mount"])
                if code:
                    raise ValueError("mount_table_unavailable")
                target = resolve_smb_path(url, output.decode("utf-8", errors="replace"))
                if target is None:
                    return self._record("nas", {"status": "mount_required", "mounted": False,
                                               "readable": False, "open_url": url}, revision)
            else:
                target = Path(path)
            from .worker_runtime import module_command
            code, output = await run_bounded(module_command('homeplane.settings', '--scan-nas', str(target)))
            report = json.loads(output) if code == 0 else {"status": "probe_failed", "readable": False}
            if url:
                report["open_url"] = url
            report.update(media_scanned=False, metadata_only=True)
        except (ValueError, OSError, TimeoutError):
            report = {"status": "probe_unavailable", "readable": False}
        return self._record("nas", report, revision)

    async def probe_camera(self) -> dict:
        revision = self.revision
        report = {"configured": bool(self.config.camera_service_url),
                  "camera_configured": bool(self.config.camera_id), "service_reachable": False,
                  "account_bound": None, "stream_verified": False, "stream_probe": "not_requested"}
        if not report["configured"]:
            return self._record("camera", {**report, "status": "not_configured"}, revision)
        try:
            if self.token_path.stat().st_size > 65536 or self.token_path.is_symlink():
                raise ValueError("invalid_token_reference")
            token = json.loads(self.token_path.read_text()).get("server", {}).get("token")
            if not isinstance(token, str) or not token or len(token) > 8192:
                return self._record("camera", {**report, "status": "service_login_required"}, revision)
            async with httpx.AsyncClient(transport=self.transport, timeout=4, trust_env=False,
                                         follow_redirects=False) as client:
                async with client.stream("GET", self.config.camera_service_url + "/api/miot/status",
                                         headers={"Authorization": "Bearer " + token}) as response:
                    report["service_reachable"] = True
                    if response.status_code in {401, 403}:
                        report["status"] = "service_login_required"
                    elif response.status_code != 200:
                        report["status"] = "service_error"
                    else:
                        content = bytearray()
                        async for chunk in response.aiter_bytes():
                            content.extend(chunk)
                            if len(content) > 65536:
                                raise ValueError("camera_status_budget")
                        body = json.loads(content)
                        bound = body.get("data", {}).get("is_bound")
                        if body.get("code") != 0 or not isinstance(bound, bool):
                            report["status"] = "service_protocol_error"
                        else:
                            report["account_bound"] = bound
                            report["status"] = "bound_stream_unverified" if bound else "account_login_required"
        except FileNotFoundError:
            report['status'] = 'service_login_required'
        except (httpx.HTTPError, OSError, ValueError, TypeError, AttributeError):
            report["status"] = "service_unavailable"
        return self._record("camera", report, revision)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--scan-nas", required=True)
    args = parser.parse_args()
    print(json.dumps(scan_directory(args.scan_nas)))
