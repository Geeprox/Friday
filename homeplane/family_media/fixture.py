"""Minimal synthetic protocol fixture, never a filesystem/NAS transport."""

import asyncio
from copy import deepcopy

from .reader import RANGE_BYTES


class SyntheticNASError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class SyntheticNASReader:
    def __init__(self):
        self.source_ref, self.epoch, self.generation = "SIMULATED-family-source", "SIMULATED-epoch-1", 1
        self.state = "online"
        self.objects, self.calls = {}, []
        self.listing = None
        self.delay = 0

    def put(self, object_ref, content, *, kind="image", closed=True, revision="SIMULATED-r1"):
        self.objects[object_ref] = {
            "entry": {
                "object_ref": object_ref,
                "kind": "file",
                "revision": revision,
                "size": len(content),
                "modified_ns": 1,
                "media_kind": kind,
                "closed": closed,
                "revision_basis": "metadata",
            },
            "bytes": content,
        }

    async def status(self):
        return {
            "source_ref": self.source_ref,
            "config_generation": self.generation,
            "connection_epoch": self.epoch,
            "state": self.state,
            "reachable": self.state == "online",
            "authenticated": self.state == "online",
            "readable": self.state == "online",
        }

    def _admit(self, source_ref):
        if source_ref != self.source_ref:
            raise SyntheticNASError("nas_source_changed")
        if self.state != "online":
            raise SyntheticNASError("nas_unavailable")

    async def list_page(self, source_ref, cursor=None, limit=128, *, directory_ref=None):
        self._admit(source_ref)
        self.calls.append(("list", cursor, limit, directory_ref))
        if self.delay:
            await asyncio.sleep(self.delay)
        if cursor and not cursor.startswith(self.epoch + ":"):
            raise SyntheticNASError("nas_cursor_expired")
        offset = int(cursor.rsplit(":", 1)[1]) if cursor else 0
        entries = [
            deepcopy(self.objects[key]["entry"])
            for key in sorted(self.objects)
            if self.listing is None or key in self.listing
        ]
        end = offset + limit
        complete = end >= len(entries)
        return {
            "source_ref": source_ref,
            "config_generation": self.generation,
            "connection_epoch": self.epoch,
            "entries": entries[offset:end],
            "next_cursor": None if complete else self.epoch + ":" + str(end),
            "enumeration_complete": complete,
            "completed_scan_token": self.epoch + ":complete" if complete else None,
            "snapshot_consistent": False,
        }

    async def stat(self, source_ref, object_ref, expected_revision=None):
        self._admit(source_ref)
        self.calls.append(("stat", object_ref))
        if object_ref not in self.objects:
            raise SyntheticNASError("nas_missing")
        entry = deepcopy(self.objects[object_ref]["entry"])
        if expected_revision and expected_revision != entry["revision"]:
            raise SyntheticNASError("nas_revision_changed")
        return entry

    async def read_range(self, source_ref, object_ref, expected_revision, offset, length):
        self._admit(source_ref)
        self.calls.append(("range", object_ref, offset, length))
        if not 0 < length <= RANGE_BYTES:
            raise SyntheticNASError("nas_budget_exceeded")
        await self.stat(source_ref, object_ref, expected_revision)
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.objects[object_ref]["bytes"][offset : offset + length]
