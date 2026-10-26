"""Bounded metadata projection of an owned worker's exact opaque snapshots.

This reads local evidence only. It neither calls the gateway nor interprets or
imports Graph nodes. Raw bytes stay private; names/enable are source metadata.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .geek_session import GeekSessionView


class GeekGraph(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(min_length=1, max_length=256)
    name: str | None = Field(default=None, max_length=128)
    source_enabled: bool | None = None
    normalization: Literal["opaque"] = "opaque"
    readable: Literal[True] = True
    writable: Literal[False] = False
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    raw_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class GeekInventoryView(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1] = 1
    session: GeekSessionView
    status: Literal["ready", "unavailable", "stale", "integrity_failed"]
    observed_at: str | None = None
    fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    graphs: list[GeekGraph] = Field(default_factory=list, max_length=64)
    coverage: Literal["geek_visible_only"] = "geek_visible_only"
    simulated: Literal[False] = False
    raw_graphs_public: Literal[False] = False
    external_rules_intercepted: Literal[False] = False


def _read(path: Path, limit: int):
    # Never follow a raw-file or parent-directory symlink. No caller-controlled
    # path is accepted; names come from the worker's constrained manifest.
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("inventory_symlink")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > limit:
            raise ValueError("inventory_file_boundary")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("inventory_file_budget")
        return data
    finally:
        os.close(fd)


def _directory(path):
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("inventory_symlink")
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("inventory_directory_boundary")


def read_inventory(root, expected, count):
    """Read <=4 manifests and <=32 MiB from the CURRENT owned session root."""
    _directory(root)
    candidates = []
    for entry in root.iterdir():
        if re.fullmatch(r"snapshot-[a-f0-9-]{36}", entry.name):
            _directory(entry)
            candidates.append(entry)
    if len(candidates) > 4:
        raise ValueError("inventory_snapshot_budget")
    for directory in sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True):
        manifest = json.loads(_read(directory / "manifest.json", 128 * 1024))
        if not isinstance(manifest, dict):
            raise ValueError("inventory_manifest_shape")
        if manifest.get("fingerprint") != expected:
            continue
        if (manifest.get("schema_version") != 1 or manifest.get("simulated") is not False
                or manifest.get("graph_semantics") != "opaque"
                or manifest.get("provider") != "oh-my-sage-readonly"):
            raise ValueError("inventory_provenance")
        observed = manifest["observed_at"]
        if not isinstance(observed, str) or datetime.fromisoformat(observed).tzinfo is None:
            raise ValueError("inventory_timestamp")
        records, fingerprints = manifest["records"], manifest["graphs"]
        if not isinstance(records, list) or len(records) > 100 or any(not isinstance(r, dict) for r in records) or not isinstance(fingerprints, dict) or len(fingerprints) != count:
            raise ValueError("inventory_manifest_budget")
        lists = [r for r in records if r.get("method") == "getGraphList"]
        graphs = [r for r in records if r.get("method") == "getGraph"]
        if len(lists) != 1 or len(graphs) != count:
            raise ValueError("inventory_graph_count")
        total = 0

        def raw_result(record):
            nonlocal total
            name = record["file"]
            if not re.fullmatch(r"[0-9]{3}-getGraph(?:List)?\.json", name):
                raise ValueError("inventory_record_path")
            data = _read(directory / name, 4 * 1024 * 1024)
            total += len(data)
            if total > 32 * 1024 * 1024 or len(data) != record["byte_count"] or hashlib.sha256(data).hexdigest() != record["raw_sha256"]:
                raise ValueError("inventory_raw_integrity")
            return json.loads(data)["result"]

        items = raw_result(lists[0])
        if not isinstance(items, list) or len(items) != count:
            raise ValueError("inventory_list_count")
        result, seen = [], set()
        for item, record in zip(items, graphs, strict=True):
            if not isinstance(item, dict):
                raise ValueError("inventory_item_shape")
            graph_id = item["id"]
            raw = raw_result(record)
            if not isinstance(graph_id, str) or graph_id in seen or not isinstance(raw, dict) or raw.get("id") != graph_id:
                raise ValueError("inventory_graph_binding")
            seen.add(graph_id)
            if record["content_sha256"] != fingerprints[graph_id]:
                raise ValueError("inventory_fingerprint_binding")
            user = item.get("userData")
            name = user.get("name") if isinstance(user, dict) else None
            # UI name is optional opaque source metadata. Never stringify nodes
            # or unrelated fields, guess an intent, or infer execution success.
            name = "".join(c for c in name if ord(c) >= 32 and ord(c) != 127)[:128] if isinstance(name, str) else None
            result.append(GeekGraph(id=graph_id, name=name or None,
                                    source_enabled=item.get("enable") if isinstance(item.get("enable"), bool) else None,
                                    fingerprint=fingerprints[graph_id], raw_sha256=record["raw_sha256"]))
        if seen != set(fingerprints):
            raise ValueError("inventory_graph_set")
        return {"observed_at": observed, "fingerprint": expected, "graphs": result}
    raise ValueError("inventory_current_snapshot_missing")
