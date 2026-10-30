"""Local bounded archive index; disk/network work never owns the control lock.

Filename times require an operator-reviewed mapping and bounded clock error.
Completion conventions are declared evidence, not independently verified hardware.
Ready means current readable, stable, valid media metadata and interval coverage;
it does not claim a successful decoder/player or a causal explanation.
"""

import asyncio
import hashlib
import json
import os
import re
import shutil
import signal
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from .archive_contracts import ArchiveResolution, ArchiveSegmentRef, ArchiveTimeRange
from .settings import resolve_smb_path, run_bounded
from .worker_runtime import module_command
from .media_tools import ffprobe_executable


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def filename_range(name, profile):
    """One observed naming shape only. No mtime, adjacent-name or duration guess."""
    match = re.fullmatch(r"[0-9]{2}_([0-9]{14})_([0-9]{14})\.(?:mp4|mov)", name, re.IGNORECASE)
    if not match:
        return None
    zone = ZoneInfo(profile["timezone"])
    values = []
    try:
        for part in match.groups():
            naive = datetime.strptime(part, "%Y%m%d%H%M%S")
            first, second = naive.replace(tzinfo=zone, fold=0), naive.replace(tzinfo=zone, fold=1)
            if (
                first.utcoffset() != second.utcoffset()
                or first.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != naive
            ):
                return None
            values.append(first.astimezone(UTC) - timedelta(seconds=profile["clock_offset_seconds"] or 0))
        return ArchiveTimeRange(start=values[0], end=values[1])
    except (ValueError, OverflowError):
        return None


def interval_gaps(request, segments):
    cursor, gaps = request.start, []
    for segment in sorted(segments, key=lambda s: (s.time_range.start, s.time_range.end)):
        if segment.status != "ready":
            continue
        error = timedelta(seconds=segment.clock_uncertainty_seconds or 0)
        start, end = (
            max(request.start, segment.time_range.start + error),
            min(request.end, segment.time_range.end - error),
        )
        if end <= start:
            continue
        if start > cursor:
            gaps.append(ArchiveTimeRange(start=cursor, end=start))
        cursor = max(cursor, end)
    if cursor < request.end:
        gaps.append(ArchiveTimeRange(start=cursor, end=request.end))
    return gaps


class ArchiveIndex:
    def __init__(self, store, settings, simulated, *, runner=None, clock=None, room_context=None):
        self.store, self.settings, self.simulated = store, settings, simulated
        if room_context is not None and not callable(room_context):
            raise ValueError("archive_room_context_unavailable")
        self.room_context = room_context
        self.runner, self.clock = runner or self._worker, clock or (lambda: datetime.now(UTC))
        self.boot_id, self.revision = str(uuid4()), 0
        self.lock, self.closed = asyncio.Lock(), False
        self.process = None
        self.last_scan, self.folders = None, []
        self.sim_anchor = self.clock().replace(microsecond=0)
        self.sim_late_arrived, self.sim_offline = False, False
        with self.store.connection:
            self.store.connection.execute(
                "CREATE TABLE IF NOT EXISTS archive_profiles (id TEXT PRIMARY KEY, source TEXT, body TEXT)"
            )
            self.store.connection.execute(
                "CREATE TABLE IF NOT EXISTS archive_files (id TEXT PRIMARY KEY, source TEXT, seen TEXT, body TEXT)"
            )
        self._prune()

    def source(self):
        return fingerprint(
            [bool(self.simulated()), self.settings.config.nas_url, self.settings.config.nas_path]
        )

    def _prune(self):
        cutoff = (self.clock() - timedelta(days=7)).isoformat()
        with self.store.connection:
            self.store.connection.execute("DELETE FROM archive_files WHERE seen < ?", (cutoff,))
            self.store.connection.execute(
                "DELETE FROM archive_files WHERE id NOT IN (SELECT id FROM archive_files ORDER BY seen DESC LIMIT 1000)"
            )

    def profiles(self):
        return [
            {"id": row[0], **json.loads(row[1])}
            for row in self.store.connection.execute(
                "SELECT id,body FROM archive_profiles WHERE source=? ORDER BY id", (self.source(),)
            )
        ]

    def all_profiles(self):
        source, result = self.source(), []
        for identifier, bound_source, raw in self.store.connection.execute(
                "SELECT id,source,body FROM archive_profiles ORDER BY id"):
            value = json.loads(raw)
            stored = value.get("room_source_context")
            current = self._room_context(value.get("room"))
            result.append({**value, "id": identifier, "source_binding_current": bound_source == source,
                           "room_binding_current": isinstance(stored, str)
                           and re.fullmatch(r"[a-f0-9]{64}", stored) is not None
                           and current is not None and stored == current})
        return result

    def _room_context(self, room):
        if self.room_context is None or not isinstance(room, str) or not room:
            return None
        try:
            value = self.room_context(room)
            return value if isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) else None
        except Exception:
            # Metadata failure never promotes an old mapping or exposes its source.
            return None

    def context(self):
        profiles = self.all_profiles()
        simulated = bool(self.simulated())
        simulated_room = self._room_context("living-room") if simulated else None
        value = fingerprint([self.boot_id, self.source(), self.revision, profiles, self.closed, simulated_room])
        if simulated:
            cameras = [
                {
                    "id": "simulated:archive:living",
                    "name": "示例客厅摄像头",
                    "room": "living-room",
                    "simulated": True,
                    "source_binding_current": True,
                    "room_binding_current": simulated_room is not None,
                    "example_ranges": {
                        "ready": self._sim_range(-120, 0).model_dump(mode="json"),
                        "pending": self._sim_range(0, 60).model_dump(mode="json"),
                        "expired": self._sim_range(-180, -120).model_dump(mode="json"),
                    },
                }
            ]
        else:
            cameras = [{k: v for k, v in p.items() if k != "relative_directory"} for p in profiles]
        fresh = bool(
            self.last_scan
            and self.last_scan["source"] == self.source()
            and (self.clock() - datetime.fromisoformat(self.last_scan["checked_at"])).total_seconds() < 60
        )
        return {
            "schema_version": 1,
            "fingerprint": value,
            "simulated": simulated,
            "source": "simulator" if simulated else "nas",
            "configured": simulated or bool(self.settings.config.nas_url or self.settings.config.nas_path),
            "cameras": cameras,
            "folders": self.folders if fresh else [],
            "scan": {k: v for k, v in self.last_scan.items() if k != "source"} if fresh else None,
            "read_only": True,
            "media_copied": False,
            "media_playback_verified": False,
            "limits": {
                "index_rows": 1000,
                "scan_files": 128,
                "scan_entries": 4096,
                "request_seconds": 1800,
                "segments": 16,
            },
            "closed": self.closed,
        }

    def _check(self, expected):
        if self.closed or expected != self.context()["fingerprint"]:
            raise ValueError("archive_context_changed")

    async def _root(self):
        path, url = self.settings.config.nas_path, self.settings.config.nas_url
        if path:
            return path
        if not url:
            raise ValueError("archive_not_configured")
        code, data = await run_bounded(["/sbin/mount"], timeout=2)
        path = resolve_smb_path(url, data.decode("utf8", errors="replace")) if code == 0 else None
        if path is None:
            raise ValueError("archive_mount_required")
        return str(path)

    async def _worker(self, root, operation, payload=None):
        env = {k: os.environ[k] for k in ("PATH", "LANG", "TMPDIR") if k in os.environ}
        env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1")
        process = await asyncio.create_subprocess_exec(
            *module_command('homeplane.archive_worker', '--root', root, '--operation', operation),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
        self.process = process
        try:
            data = json.dumps(payload or {}, allow_nan=False).encode() if payload is not None else b""
            if len(data) > 16384:
                raise ValueError("archive_job_budget")
            async with asyncio.timeout(6):
                process.stdin.write(data)
                await process.stdin.drain()
                process.stdin.close()
                output = await process.stdout.read(65537)
                if len(output) > 65536:
                    raise ValueError("archive_output_budget")
                await process.wait()
            if process.returncode:
                raise ValueError("archive_worker_failed")
            return json.loads(output)
        finally:
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
            if self.process is process:
                self.process = None

    async def scan(self, expected):
        if self.lock.locked():
            raise ValueError("archive_busy")
        async with self.lock:
            self._check(expected)
            if self.simulated():
                return self.context()
            root = await self._root()
            self._check(expected)
            report = await self.runner(root, "scan")
            self._check(expected)
            if report.get("status") != "readable":
                raise ValueError("archive_unavailable")
            if len(report.get("files", [])) > 128 or len(report.get("folders", [])) > 16:
                raise ValueError("archive_worker_contract")
            now, source = self.clock(), self.source()
            folders = [
                {
                    "folder_ref": fingerprint([source, f["relative_path"]]),
                    "label": f"录像目录 {n + 1}",
                    "scan_complete": bool(f["scan_complete"]),
                }
                for n, f in enumerate(report["folders"])
            ]
            for public, private in zip(folders, report["folders"]):
                public["sample_filenames"] = [
                    f["filename"] for f in report["files"] if f["folder"] == private["relative_path"]
                ][:3]
            with self.store.connection:
                for item in report["files"]:
                    identifier = fingerprint([source, item["relative_path"]])
                    old = self.store.connection.execute(
                        "SELECT body FROM archive_files WHERE id=?", (identifier,)
                    ).fetchone()
                    previous = json.loads(old[0]) if old else None
                    item = {
                        **item,
                        "folder_ref": fingerprint([source, item["folder"]]),
                        "stable_since": previous["stable_since"]
                        if previous and previous["signature"] == item["signature"]
                        else now.isoformat(),
                    }
                    if len(json.dumps(item).encode()) > 4096:
                        raise ValueError("archive_record_budget")
                    self.store.connection.execute(
                        "INSERT OR REPLACE INTO archive_files VALUES (?,?,?,?)",
                        (identifier, source, now.isoformat(), json.dumps(item)),
                    )
            self._prune()
            self.folders = folders
            self.last_scan = {
                "source": source,
                "checked_at": now.isoformat(),
                "scan_complete": bool(report["scan_complete"]),
                "indexed_this_scan": len(report["files"]),
                "media_read": False,
            }
            self.revision += 1
            return self.context()

    def save_camera(self, edit):
        self._check(edit.expected_context)
        if self.lock.locked() or self.simulated():
            raise ValueError("archive_busy_or_simulated")
        if edit.folder_ref not in {f["folder_ref"] for f in self.context()["folders"]}:
            raise ValueError("archive_folder_review_expired")
        row = self.store.connection.execute(
            "SELECT body FROM archive_files WHERE source=?", (self.source(),)
        ).fetchall()
        match = next(
            (json.loads(r[0]) for r in row if json.loads(r[0])["folder_ref"] == edit.folder_ref), None
        )
        if not match:
            raise ValueError("archive_folder_has_no_sample")
        folder_owner = next((p for p in self.profiles() if p["folder_ref"] == edit.folder_ref), None)
        old = (
            next((p for p in self.all_profiles() if p["id"] == edit.camera_id), None)
            if edit.camera_id
            else folder_owner
        )
        if edit.camera_id and old is None:
            raise ValueError("archive_camera_not_found")
        if folder_owner and old and folder_owner["id"] != old["id"]:
            raise ValueError("archive_folder_already_bound")
        if (
            not old
            and self.store.connection.execute("SELECT count(*) FROM archive_profiles").fetchone()[0] >= 16
        ):
            raise ValueError("archive_camera_capacity")
        identifier = old["id"] if old else "archive-camera:" + str(uuid4())
        value = edit.model_dump(exclude={"expected_context", "camera_id"})
        value.update(
            relative_directory=match["folder"],
            room_source_context=self._room_context(edit.room),
            mapping_evidence="operator_declared_not_hardware_verified",
            simulated=False,
        )
        with self.store.connection:
            self.store.connection.execute(
                "INSERT OR REPLACE INTO archive_profiles VALUES (?,?,?)",
                (identifier, self.source(), json.dumps(value)),
            )
        self.revision += 1
        return self.context()

    def _sim_range(self, start, end):
        return ArchiveTimeRange(
            start=self.sim_anchor + timedelta(seconds=start), end=self.sim_anchor + timedelta(seconds=end)
        )

    def simulate(self, expected, scenario):
        self._check(expected)
        if not self.simulated() or scenario not in {"late-arrives", "offline", "reset"}:
            raise ValueError("archive_simulator_required")
        self.sim_late_arrived = scenario == "late-arrives"
        self.sim_offline = scenario == "offline"
        self.revision += 1
        return self.context()

    async def resolve(self, request):
        if self.lock.locked():
            raise ValueError("archive_busy")
        async with self.lock:
            self._check(request.expected_context)
            simulated = bool(self.simulated())
            if simulated:
                return self._resolve_simulated(request)
            profile = next((p for p in self.profiles() if p["id"] == request.camera_id), None)
            if not profile:
                return self._answer(request, "unavailable", "archive_camera_mapping_required", [])
            if not profile["timing_reviewed"]:
                return self._answer(request, "unavailable", "archive_recording_clock_unreviewed", [])
            selected, unknown_time = [], False
            for row in self.store.connection.execute(
                "SELECT body FROM archive_files WHERE source=?", (self.source(),)
            ):
                item = json.loads(row[0])
                if item["folder_ref"] != profile["folder_ref"]:
                    continue
                span = filename_range(item["filename"], profile)
                if span is None:
                    unknown_time = True
                if span and span.end > request.time_range.start and span.start < request.time_range.end:
                    selected.append((item, span))
            if len(selected) > 16:
                return self._answer(request, "unavailable", "archive_segment_budget", [])
            root = await self._root()
            self._check(request.expected_context)
            report = await self.runner(
                root,
                "inspect",
                {"paths": [i[0]["relative_path"] for i in selected], "ffprobe": ffprobe_executable()},
            )
            self._check(request.expected_context)
            if report.get("status") != "readable":
                return self._answer(request, "unavailable", "archive_unavailable", [])
            rows = {r["relative_path"]: r for r in report.get("files", [])}
            if set(rows) != {i[0]["relative_path"] for i in selected}:
                raise ValueError("archive_worker_contract")
            segments = []
            for item, span in selected:
                stat = rows[item["relative_path"]]
                unchanged = (
                    stat.get("signature") == item["signature"] and stat.get("unchanged_during_probe") is True
                )
                stable = (self.clock() - datetime.fromisoformat(item["stable_since"])).total_seconds() >= 2
                valid = (
                    stat.get("media_metadata_valid") is True
                    and abs(stat.get("duration_seconds", 0) - (span.end - span.start).total_seconds()) <= 2
                )
                if stat.get("present") is False:
                    status, reason = "expired", "archive_previously_indexed_file_absent"
                elif stat.get("present") is not True:
                    status, reason = "unavailable", "archive_file_unavailable"
                elif not unchanged or not stable or not stat.get("size_bytes"):
                    status, reason = "pending", "archive_file_still_changing"
                elif not valid:
                    status, reason = "pending", "archive_media_time_or_container_unvalidated"
                elif profile["completion_convention"] == "unreviewed":
                    status, reason = "pending", "archive_completion_unreviewed"
                else:
                    status, reason = "ready", "archive_current_metadata_ready_playback_unverified"
                segments.append(
                    ArchiveSegmentRef(
                        media_ref=fingerprint([profile["id"], span.model_dump(mode="json")]),
                        camera_id=profile["id"],
                        time_range=span,
                        status=status,
                        reason=reason,
                        file_signature=stat.get("signature"),
                        size_bytes=stat.get("size_bytes"),
                        audio_track=stat.get("audio_track"),
                        codecs=stat.get("codecs", []),
                        media_time_bases=stat.get("media_time_bases", []),
                        clock_offset_seconds=profile["clock_offset_seconds"],
                        clock_uncertainty_seconds=profile["clock_uncertainty_seconds"],
                        time_basis="operator_reviewed_filename",
                        completion_basis="operator_reviewed_convention"
                        if profile["completion_convention"] != "unreviewed"
                        else "unverified",
                        simulated=False,
                    )
                )
            fresh = self.context()["scan"]
            complete = bool(
                fresh
                and not unknown_time
                and any(
                    f["folder_ref"] == profile["folder_ref"] and f["scan_complete"]
                    for f in self.context()["folders"]
                )
            )
            return self._finish(request, segments, complete, profile)

    def _resolve_simulated(self, request):
        if request.camera_id != "simulated:archive:living" or self.sim_offline:
            return self._answer(request, "unavailable", "simulated_archive_offline_or_unknown_camera", [])
        segments = []
        for start, end, status in [
            (-180, -120, "expired"),
            (-120, -60, "ready"),
            (-60, 0, "ready"),
            (0, 60, "ready" if self.sim_late_arrived else "pending"),
        ]:
            span = self._sim_range(start, end)
            if span.start >= request.time_range.end or span.end <= request.time_range.start:
                continue
            segments.append(
                ArchiveSegmentRef(
                    media_ref=fingerprint([request.camera_id, span.model_dump(mode="json")]),
                    camera_id=request.camera_id,
                    time_range=span,
                    status=status,
                    reason="simulated_archive_" + status,
                    clock_offset_seconds=0,
                    clock_uncertainty_seconds=0,
                    time_basis="simulated",
                    completion_basis="simulated",
                    simulated=True,
                )
            )
        return self._finish(request, segments, True, None)

    def _finish(self, request, segments, complete, profile):
        ordered = sorted(segments, key=lambda s: s.time_range.start)
        if any(a.time_range.end > b.time_range.start for a, b in zip(ordered, ordered[1:])):
            return self._answer(
                request, "unavailable", "archive_overlapping_segments_ambiguous", segments, complete
            )
        gaps = interval_gaps(request.time_range, segments)
        if not gaps:
            status, reason = "ready", "archive_interval_covered_playback_unverified"
        elif any(s.status == "unavailable" for s in segments):
            status, reason = "unavailable", "archive_partial_unavailable"
        elif any(s.status == "pending" for s in segments):
            status, reason = "pending", "archive_waiting_for_complete_segments"
        elif any(s.status == "expired" for s in segments):
            status, reason = "expired", "archive_source_segment_expired"
        elif segments and any(s.status == "ready" and s.clock_uncertainty_seconds for s in segments):
            status, reason = "unavailable", "archive_coverage_or_clock_boundary_uncertain"
        elif not complete:
            status, reason = "unavailable", "archive_index_coverage_incomplete"
        elif (
            profile
            and profile["arrival_delay_seconds"] is not None
            and self.clock() < request.time_range.end + timedelta(seconds=profile["arrival_delay_seconds"])
        ):
            status, reason = "pending", "archive_within_declared_arrival_delay"
        else:
            status, reason = "missing", "archive_no_known_segment_or_clock_boundary_gap"
        return self._answer(request, status, reason, segments, complete, gaps)

    def _answer(self, request, status, reason, segments, complete=False, gaps=None):
        return ArchiveResolution(
            context_fingerprint=self.context()["fingerprint"],
            camera_id=request.camera_id,
            time_range=request.time_range,
            status=status,
            reason=reason,
            segments=segments,
            gaps=gaps if gaps is not None else [request.time_range],
            simulated=bool(self.simulated()),
            source_scan_complete=complete,
        )

    async def close(self):
        self.closed = True
        if self.process and self.process.returncode is None:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await self.process.wait()

    async def playback_binding(self, request):
        """Private binding from current camera/time evidence; never an API file path."""
        report = await self.resolve(request)
        if report.status != "ready":
            raise ValueError("archive_playback_not_ready")
        segment = next((s for s in report.segments if s.media_ref == request.media_ref), None)
        if (
            segment is None
            or request.time_range.start < segment.time_range.start
            or request.time_range.end > segment.time_range.end
        ):
            raise ValueError("archive_playback_interval_mismatch")
        if self.lock.locked():
            raise ValueError("archive_busy")
        async with self.lock:
            self._check(request.expected_context)
            if self.simulated():
                root = str(Path(__file__).resolve().parents[1] / "fixtures" / "homeplane")
                relative = "archive-simulated.mp4"
            else:
                profile = next(p for p in self.profiles() if p["id"] == request.camera_id)
                matches = []
                for row in self.store.connection.execute(
                    "SELECT body FROM archive_files WHERE source=?", (self.source(),)
                ):
                    item = json.loads(row[0])
                    span = filename_range(item["filename"], profile)
                    if (
                        item["folder_ref"] == profile["folder_ref"]
                        and span
                        and fingerprint([profile["id"], span.model_dump(mode="json")]) == request.media_ref
                    ):
                        matches.append(item)
                if len(matches) != 1:
                    raise ValueError("archive_playback_binding_ambiguous")
                root, relative = await self._root(), matches[0]["relative_path"]
            self._check(request.expected_context)
            start = (request.time_range.start - segment.time_range.start).total_seconds()
            end = (request.time_range.end - segment.time_range.start).total_seconds()
            info = await self.runner(
                root,
                "playback-info",
                {"path": relative, "ffprobe": ffprobe_executable(), "offset_seconds": start},
            )
            self._check(request.expected_context)
            if (
                info.get("status") != "readable"
                or not info.get("media_metadata_valid")
                or info.get("media_start_seconds") != 0
            ):
                raise ValueError("archive_playback_media_unqualified")
            if not self.simulated() and info.get("signature") != segment.file_signature:
                raise ValueError("archive_source_changed")
            if (
                not 0 < info.get("size_bytes", 0) <= 1073741824
                or abs(
                    info["duration_seconds"]
                    - (segment.time_range.end - segment.time_range.start).total_seconds()
                )
                > 2
                or end > info["duration_seconds"]
            ):
                raise ValueError("archive_playback_media_budget_or_timeline")
            return {
                "root": root,
                "path": relative,
                "signature": info["signature"],
                "size_bytes": info["size_bytes"],
                "segment": segment,
                "start": start,
                "end": end,
                "info": info,
            }
