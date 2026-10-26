"""Bounded preinstalled local WAV prompts through the actual afplay adapter.

No voice/model download, discovery service, cloud TTS or default room mapping.
An exit code describes an output call, never room audibility or echo admission.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import os
import tempfile
import time
import wave
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Mapping

from .feedback_contracts import PROMPTS, LocalOutputReport, PlaybackWindow

AFPLAY = Path("/usr/bin/afplay")


@dataclass(frozen=True)
class PromptAsset:
    path: Path
    sha256: str


@dataclass(frozen=True)
class LocalPromptInstallation:
    """Private owner evidence; constructing a receipt does not qualify a room."""

    root: Path
    executable_sha256: str
    prompts: Mapping[str, PromptAsset]
    locality_verified: bool = False
    reviewed_fixed_prompts: bool = False


class MacLocalOutput:
    def __init__(self, installation: LocalPromptInstallation | None = None, *, spawn=None,
                 simulated=False, window_hook: Callable[[PlaybackWindow], None] | None = None,
                 monotonic=time.monotonic, cleanup_timeout=0.25):
        if spawn is not None and simulated is not True:
            raise ValueError("custom_output_runner_requires_simulated")
        if type(simulated) is not bool or not 0 < cleanup_timeout <= 1:
            raise ValueError("invalid_output_lifecycle")
        self.installation = installation
        self.spawn = spawn or asyncio.create_subprocess_exec
        self.simulated = simulated
        self.window_hook, self.monotonic = window_hook, monotonic
        self.cleanup_timeout = cleanup_timeout
        self._active = None
        self._generation = 0
        self.closed = False

    def _asset(self, prompt_id):
        installation = self.installation
        if not installation or installation.locality_verified is not True or installation.reviewed_fixed_prompts is not True:
            raise ValueError("local_output_installation_unqualified")
        if prompt_id not in PROMPTS or prompt_id not in installation.prompts:
            raise ValueError("local_prompt_unavailable")
        if not AFPLAY.is_file() or not os.access(AFPLAY, os.X_OK):
            raise ValueError("local_output_executable_unavailable")
        if hashlib.sha256(AFPLAY.read_bytes()).hexdigest() != installation.executable_sha256:
            raise ValueError("local_output_installation_changed")
        asset = installation.prompts[prompt_id]
        path, root = Path(asset.path), Path(installation.root)
        if not path.is_absolute() or not root.is_absolute() or path.is_symlink() or any(p.is_symlink() for p in path.parents):
            raise ValueError("local_prompt_path_unqualified")
        if not path.is_relative_to(root) or not path.is_file() or not 44 <= path.stat().st_size <= 1048576:
            raise ValueError("local_prompt_unavailable")
        data = path.read_bytes()
        if len(data) > 1048576 or hashlib.sha256(data).hexdigest() != asset.sha256:
            raise ValueError("local_prompt_changed")
        try:
            with wave.open(io.BytesIO(data), "rb") as reader:
                if (reader.getcomptype() != "NONE" or reader.getnchannels() not in {1, 2}
                    or reader.getsampwidth() != 2 or not 8000 <= reader.getframerate() <= 48000
                    or not 0 < reader.getnframes() / reader.getframerate() <= 8):
                    raise ValueError("local_prompt_format_unqualified")
        except (wave.Error, EOFError) as exc:
            raise ValueError("local_prompt_format_unqualified") from exc
        return path

    @staticmethod
    def _pin_asset(path, expected_sha256):
        # Verify the exact bounded byte image, then remove its filesystem name
        # and keep only our read-only descriptor across the async spawn gap.
        with path.open("rb") as reader:
            data = reader.read(1048577)
        if not 44 <= len(data) <= 1048576 or hashlib.sha256(data).hexdigest() != expected_sha256:
            raise ValueError("local_prompt_changed")
        writable, temporary = tempfile.mkstemp(prefix="friday-fixed-prompt-", suffix=".wav")
        pinned = None
        try:
            with os.fdopen(writable, "wb") as writer:
                writer.write(data)
            pinned = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW)
            if hashlib.sha256(os.pread(pinned, 1048577, 0)).hexdigest() != expected_sha256:
                raise ValueError("local_prompt_changed")
            os.unlink(temporary)
            return pinned
        except BaseException:
            if pinned is not None:
                os.close(pinned)
            raise
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _settle(self, entry):
        entry["settled"] = True
        descriptor = entry.pop("asset_fd", None)
        if descriptor is not None:
            os.close(descriptor)
        if self._active is entry:
            self._active = None
        self._finish_window(entry)

    @staticmethod
    def _current(check):
        try:
            return check() is True
        except Exception:
            return False

    def _observe(self, window):
        if self.window_hook:
            self.window_hook(window)

    def _start_cleanup(self, entry):
        task = entry.get("cleanup_task")
        retry = task is not None and task.done() and not task.cancelled() and task.exception() is None and task.result() == "pending"
        if task is None or retry:
            task = asyncio.create_task(self._reap(entry), name="home-flow-local-output-cleanup")
            task.add_done_callback(lambda finished: None if finished.cancelled() else finished.exception())
            entry["cleanup_task"] = task
        return task

    async def _cleanup(self, entry):
        # Cancellation and playback completion join the same owned reaper.
        return await asyncio.shield(self._start_cleanup(entry))

    def _waiter(self, entry, process):
        waiter = entry.get("wait")
        if waiter is None:
            waiter = entry["wait"] = asyncio.create_task(process.wait(), name="home-flow-local-output-wait")
            waiter.add_done_callback(lambda _: self._late_wait(entry))
        return waiter

    def _late_wait(self, entry):
        if not entry.get("abandoned") or entry.get("settled"):
            return
        cleanup = entry.get("cleanup_task")
        if cleanup and not cleanup.done():
            # The single current reaper may just be returning pending. Join it
            # before retrying this one completed waiter; never overlap reapers.
            if not entry.get("late_wait_join_armed"):
                entry["late_wait_join_armed"] = True

                def after_cleanup(_):
                    entry["late_wait_join_armed"] = False
                    self._late_wait(entry)

                cleanup.add_done_callback(after_cleanup)
        else:
            self._start_cleanup(entry)

    async def _reap(self, entry):
        spawn = entry["spawn"]
        if not spawn.done():
            done, _ = await asyncio.wait({spawn}, timeout=self.cleanup_timeout)
            if not done:
                return "pending"
        try:
            process = spawn.result()
        except (Exception, asyncio.CancelledError):
            self._settle(entry)
            return "not_needed"
        entry["process"] = process
        if process.returncode is None and not entry.get("terminate_attempted"):
            entry["terminate_attempted"] = True
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        waiter = self._waiter(entry, process)
        done, _ = await asyncio.wait({waiter}, timeout=self.cleanup_timeout)
        if not done and not entry.get("kill_attempted"):
            entry["kill_attempted"] = True
            try:
                process.kill()
            except ProcessLookupError:
                pass
        if not done:
            done, _ = await asyncio.wait({waiter}, timeout=self.cleanup_timeout)
        if not done:
            return "pending"
        if waiter.cancelled() or waiter.exception() is not None or type(waiter.result()) is not int or process.returncode is None:
            return "pending"  # A done/failed waiter alone does not prove a joined child.
        self._settle(entry)
        return "reaped"

    def _finish_window(self, entry):
        window = entry.get("window")
        if window is not None and window.ended_mono is None:
            entry["window"] = replace(window, ended_mono=self.monotonic(), outcome=entry.get("outcome", "cancelled"))
            try:
                self._observe(entry["window"])
            except Exception:
                entry["window_hook_failed"] = True

    def _late_spawn(self, entry):
        # A spawn finishing after cancellation is still owned and gets reaped.
        if entry.get("abandoned") and not entry.get("settled") and entry.get("process") is None:
            cleanup = entry.get("cleanup_task")
            if cleanup and not cleanup.done():
                cleanup.add_done_callback(lambda _: self._late_spawn(entry))
            else:
                self._start_cleanup(entry)

    async def play(self, *, operation_id, room_id, endpoint_id, prompt_id, volume, deadline_mono, is_current):
        if self.closed:
            return LocalOutputReport("unavailable", "local_output_closed", endpoint_id, simulated=self.simulated)
        if self._active is not None:
            return LocalOutputReport("unavailable", "local_output_cleanup_or_playback_pending", endpoint_id,
                                     simulated=self.simulated)
        if type(volume) not in {int, float} or not 0 <= volume <= 1:
            return LocalOutputReport("unavailable", "invalid_output_volume", endpoint_id, simulated=self.simulated)
        if volume == 0:
            return LocalOutputReport("quiet", "output_muted", endpoint_id, simulated=self.simulated)
        if deadline_mono <= self.monotonic() or not self._current(is_current):
            return LocalOutputReport("cancelled", "output_expired_or_context_changed", endpoint_id,
                                     simulated=self.simulated)
        try:
            asset = self._asset(prompt_id)
            expected_asset = self.installation.prompts[prompt_id]
            executable = AFPLAY
        except (ValueError, OSError) as exc:
            code = str(exc) if isinstance(exc, ValueError) else "local_prompt_unavailable"
            return LocalOutputReport("unavailable", code, endpoint_id, simulated=self.simulated)
        # Check again after bounded local installation reads, immediately before spawn.
        if deadline_mono <= self.monotonic() or not self._current(is_current):
            return LocalOutputReport("cancelled", "output_expired_or_context_changed", endpoint_id,
                                     configured=True, simulated=self.simulated, audibility="unverified")
        generation = self._generation
        window = PlaybackWindow(operation_id, room_id, endpoint_id, prompt_id, self.monotonic(),
                                simulated=self.simulated)
        try:
            self._observe(window)
        except Exception:
            return LocalOutputReport("unavailable", "playback_window_hook_failed", endpoint_id,
                                     configured=True, simulated=self.simulated, audibility="unverified")

        async def spawn_current():
            # Fence the actual dispatch, after hooks and task scheduling. A spawn
            # already in progress stays owned for late-child cleanup.
            if (self.closed or entry["abandoned"] or generation != self._generation
                or deadline_mono <= self.monotonic() or not self._current(is_current)):
                raise asyncio.CancelledError
            if self._asset(prompt_id) != asset or self.installation.prompts[prompt_id] != expected_asset or AFPLAY != executable:
                raise ValueError("local_prompt_changed")
            descriptor = entry["asset_fd"] = self._pin_asset(asset, expected_asset.sha256)
            if (self.closed or entry["abandoned"] or generation != self._generation
                or deadline_mono <= self.monotonic() or not self._current(is_current)):
                raise asyncio.CancelledError
            entry["dispatched"] = True
            return await self.spawn(
                str(executable), "-v", f"{volume:.3f}", f"/dev/fd/{descriptor}", pass_fds=(descriptor,),
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}, start_new_session=True,
            )

        spawn = asyncio.create_task(spawn_current(), name="home-flow-local-output-spawn")
        entry = {"operation_id": operation_id, "spawn": spawn, "process": None, "abandoned": False,
                 "window": window, "outcome": "attempted", "dispatched": False}
        self._active = entry
        spawn.add_done_callback(lambda _: self._late_spawn(entry))
        status, code, completed, cleanup = "failed", "local_output_failed", False, "not_needed"
        try:
            done, _ = await asyncio.wait({spawn}, timeout=max(0, min(10, deadline_mono - self.monotonic())))
            if not done:
                status, code = "timeout", "local_output_spawn_timeout"
            else:
                process = entry["process"] = spawn.result()
                waiter = self._waiter(entry, process)
                while True:
                    if generation != self._generation or not self._current(is_current):
                        status, code = "cancelled", "output_cancelled_or_context_changed"
                        break
                    remaining = min(deadline_mono, window.started_mono + 10) - self.monotonic()
                    if remaining <= 0:
                        status, code = "timeout", "local_output_timeout"
                        break
                    done, _ = await asyncio.wait({waiter}, timeout=min(0.05, remaining))
                    if done:
                        exit_code = waiter.result()
                        completed = exit_code == 0
                        status = "completed" if completed else "failed"
                        code = "output_call_completed" if completed else "local_output_failed"
                        break
        except asyncio.CancelledError:
            status, code = "cancelled", "output_cancelled"
        except ValueError as exc:
            status = "unavailable" if not entry["dispatched"] else "failed"
            code = str(exc) if str(exc).startswith("local_prompt_") or str(exc).startswith("local_output_installation_") else "local_output_failed"
        except Exception:
            status, code = "failed", "local_output_failed"
        finally:
            entry["abandoned"] = True
            entry["outcome"] = status
            cleanup = await self._cleanup(entry)
            window = entry["window"]
            if cleanup == "pending":
                window = entry["window"] = replace(window, outcome="cleanup_pending")
                try:
                    self._observe(window)
                except Exception:
                    entry["window_hook_failed"] = True
            if entry.get("window_hook_failed"):
                code = "playback_window_hook_failed"
        return LocalOutputReport(status, code, endpoint_id, True, entry["dispatched"], completed, self.simulated,
                                 "unverified", cleanup, window)

    async def cancel(self, operation_id):
        entry = self._active
        if not entry or entry["operation_id"] != operation_id:
            return "not_needed"
        self._generation += 1
        entry["abandoned"] = True
        entry["outcome"] = "cancelled"
        return await self._cleanup(entry)

    async def close(self):
        self.closed = True
        if self._active:
            return await self.cancel(self._active["operation_id"])
        return "not_needed"
