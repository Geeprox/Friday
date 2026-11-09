"""Opt-in, session-scoped host output and recoverable light scenes.

The public game script is the only speech input. Private roles/actions never
cross this port. Device writes reuse Friday's existing live authority and CAS.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import httpx

from .action_contracts import DevicePlanRequest
from .ha_control import HADeviceWriter


class SceneLights:
    @staticmethod
    def _state(value):
        if not isinstance(value,dict):return False
        return (value=={'power':'off'} or set(value)=={'power','brightness'} and value['power']=='on'
                and type(value['brightness']) is int and 1<=value['brightness']<=255)

    @classmethod
    def _record(cls,value):
        if not isinstance(value,dict):return False
        binding=value.get('binding')
        return (isinstance(binding,str) and len(binding)==64 and all(c in '0123456789abcdef'for c in binding)
            and cls._state(value.get('original')) and cls._state(value.get('expected'))
            and ('previous'not in value or cls._state(value['previous']))
            and value.get('status') in {'selected','sending','planned','sent','accepted','reported_match',
                'unknown_outcome','failed','rejected','expired','suspended','left_unchanged'}
            and ('attempt'not in value or type(value['attempt']) is int and value['attempt']>=0)
            and ('released'not in value or type(value['released']) is bool))

    def __init__(self, core, lock, journal: Path):
        self.core, self.lock, self.journal = core, lock, Path(journal)
        self.operation_lock = asyncio.Lock()
        self.records = {}
        self.session = None
        self.receipts = []
        self.recovery = False
        try:
            if self.journal.stat().st_size > 32768:
                raise ValueError('invalid_scene_journal')
            stored = json.loads(self.journal.read_text())
            if not isinstance(stored,dict) or stored.get("version") != 1 or not isinstance(stored.get("records"), dict) or len(stored["records"]) > 12:
                raise ValueError("invalid_scene_journal")
            if (stored['records'] and (not isinstance(stored.get('session'),str) or not 1<=len(stored['session'])<=128)
                    or any(not isinstance(k,str) or not 1<=len(k)<=512 or not self._record(v)
                           for k,v in stored['records'].items())):
                raise ValueError('invalid_scene_journal')
            self.records = stored["records"]
            self.session = stored["session"]
            self.recovery = bool(self.records)
        except FileNotFoundError:
            pass
        except (ValueError, KeyError, TypeError, OSError):
            # Preserve an unreadable recovery file; never overwrite its evidence.
            self.recovery = self.journal.exists()

    def _save(self):
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        temp = self.journal.with_suffix(".pending")
        payload = dict(version=1, session=self.session, records=self.records)
        with open(temp, "w", opener=lambda p, flags: os.open(p, flags | os.O_NOFOLLOW, 0o600)) as output:
            os.fchmod(output.fileno(), 0o600)
            json.dump(payload, output)
            output.flush()
            os.fsync(output.fileno())
        temp.replace(self.journal)

    def status(self):
        pending = self.recovery or any(not record.get('released') and record.get('status') in {
            'sending','unknown_outcome','suspended','failed','rejected','expired'} for record in self.records.values())
        return dict(selected=len(self.records), recovery_pending=pending,
                    receipts=self.receipts[-12:], physical_effect_verified=False)

    def _release(self, target):
        record = self.records.pop(target)
        try:
            self._save()
        except BaseException:
            # A failed journal commit cannot erase our in-memory responsibility.
            self.records[target] = record
            self.recovery = True
            raise

    async def choices(self):
        snapshot = await self.core.snapshot()
        targets = set(self.core.actions.targets(snapshot))
        result = []
        for e in snapshot.entities:
            cap = next((c for c in e.capabilities if c.name == "light.set" and c.access == "write"
                        and c.support == "supported" and c.minimum is not None and c.maximum is not None), None)
            if e.kind == "light" and e.id in targets and cap:
                result.append(dict(id=e.id, name=e.name, room=e.room))
        return result

    async def _read(self, target, fingerprint=None):
        snapshot = await self.core.snapshot()
        entity = next((e for e in snapshot.entities if e.id == target), None)
        if entity is None or target not in self.core.actions.targets(snapshot):
            raise ValueError("scene_light_unavailable")
        binding = self.core.actions.binding_fingerprint(entity)
        if fingerprint is not None and binding != fingerprint:
            raise ValueError("scene_light_binding_changed")
        async with asyncio.timeout(4):
            value = await HADeviceWriter(self.core.adapter, entity, "light.set").readback()
        if value.get("power") not in {"on", "off"}:
            raise ValueError("scene_light_readback_unavailable")
        if value["power"] == "on" and (type(value.get("brightness")) is not int or not 1 <= value["brightness"] <= 255):
            raise ValueError("scene_light_brightness_unavailable")
        return entity, binding, {k: v for k, v in value.items() if k == "power" or k == "brightness" and value["power"] == "on"}

    async def select(self, session, targets):
        async with self.operation_lock:
            await self._select(session, targets)

    async def _select(self, session, targets):
        if self.records or self.recovery or self.journal.exists() and not self.session:
            raise ValueError("scene_recovery_required")
        allowed = {item["id"] for item in await self.choices()}
        if len(targets) > 12 or len(set(targets)) != len(targets) or not set(targets) <= allowed:
            raise ValueError("scene_selection_invalid")
        records = {}
        for target in targets:
            _, binding, value = await self._read(target)
            records[target] = dict(binding=binding, original=value, expected=value, status="selected")
        self.records, self.session = records, session
        if records:
            self._save()

    async def _write(self, target, record, desired, event, current):
        async with self.lock:
            entity, _, reported = await self._read(target, record["binding"])
            if reported != record["expected"]:
                raise ValueError("scene_light_manually_changed")
            if current() is not True:
                raise ValueError("scene_expired")
            if reported == desired:
                return dict(target=target, status='reported_match', physical_effect_verified=False)
            attempt = record.get('attempt', 0) + 1
            request = DevicePlanRequest(target=target, operation="light.set", desired=desired,
                                        expected_state_version=entity.state_version,
                                        expires_at=datetime.now(UTC) + timedelta(seconds=15),
                                        idempotency_key=hashlib.sha256(f"{self.session}:{event}:{attempt}:{target}".encode()).hexdigest())
            trace = await self.core.actions.plan(request)
            if trace.status != "planned":
                raise ValueError("scene_light_authority_unavailable")
            # Persist before the send claim. An interrupted send must leave a
            # recoverable record, rather than silently losing the original state.
            previous = dict(record['expected'])
            record.update(expected=desired, previous=previous, status="sending", attempt=attempt,
                          action_id=trace.action_id)
            try:
                self._save()
                trace = await self.core.actions.execute(trace.action_id, dispatch_guard=current)
            except BaseException:
                outcome = self.core.store.light_action(trace.action_id)
                unsent = outcome is not None and (outcome.status in {'planned','rejected','expired'}
                    or outcome.status=='failed' and outcome.code=='rejected_by_ha')
                record.update(expected=previous if unsent else desired,
                              status='selected' if unsent else 'unknown_outcome')
                if not unsent:self.recovery=True
                try:self._save()
                except OSError:self.recovery=True
                raise
            record["status"] = trace.status
            if trace.status in {'rejected', 'expired'} or trace.status=='failed' and trace.code=='rejected_by_ha':
                record['expected'] = previous
            self._save()
            return dict(target=target, status=trace.status, physical_effect_verified=False)

    async def scene(self, night, revision, current):
        async with self.operation_lock:
            await self._scene(night, revision, current)

    async def _scene(self, night, revision, current):
        for target, record in list(self.records.items()):
            if record.get("released") or record.get("status") in {
                    'sending','suspended','unknown_outcome','failed','rejected','expired'}:
                continue
            # Never black out the room and never flash. The elected light scene
            # uses only admitted brightness capability; it does not invent RGB.
            desired = dict(power="on", brightness=46 if night else 179)
            try:
                receipt = await self._write(target, record, desired, f"phase-{revision}", current)
            except (ValueError, OSError, TimeoutError, httpx.HTTPError) as exc:
                manual = str(exc) in {"scene_light_manually_changed", "scene_light_binding_changed"}
                stale = str(exc) in {'scene_expired','dispatch_guard_rejected'}
                record["released"] = manual
                record["status"] = 'left_unchanged' if manual else 'selected' if stale else 'suspended'
                if not manual and not stale:self.recovery=True
                self._save()
                receipt = dict(target=target, status=record["status"], code=str(exc)[:80] if isinstance(exc, ValueError) else "light_transport_unavailable")
            self.receipts.append(receipt)

    async def restore(self, *, current=lambda: True):
        async with self.operation_lock:
            if current() is not True:
                raise ValueError('scene_expired')
            return await self._restore(current)

    async def _restore(self, current):
        if self.recovery and not self.records:
            raise ValueError("scene_recovery_file_unreadable")
        for target, record in list(self.records.items()):
            if current() is not True:
                raise ValueError('scene_expired')
            if record.get("released"):
                self.receipts.append(dict(target=target, status="left_unchanged"))
                self._release(target)
                continue
            try:
                _, _, value = await self._read(target, record["binding"])
                if current() is not True:
                    raise ValueError('scene_expired')
                if record.get('status') in {'sending', 'unknown_outcome'} and value == record.get('previous'):
                    # A cancelled/uncertain send may still arrive later. Seeing
                    # the old value once is insufficient to erase its journal.
                    continue
                if value == record["original"]:
                    receipt = dict(target=target, status="reported_match", physical_effect_verified=False)
                else:
                    receipt = await self._write(target, record, record["original"], "restore", current)
            except ValueError as exc:
                if str(exc) == 'scene_expired':
                    raise
                if str(exc) in {"scene_light_manually_changed", "scene_light_binding_changed"}:
                    receipt = dict(target=target, status="left_unchanged", code=str(exc))
                else:
                    continue
            except (OSError, TimeoutError, httpx.HTTPError):
                continue
            self.receipts.append(receipt)
            if receipt["status"] in {"reported_match", "left_unchanged"}:
                self._release(target)
        self.recovery = bool(self.records)
        if not self.records:
            self.journal.unlink(missing_ok=True)
            self.session = None
        return self.status()


class HostEffects:
    def __init__(self, lights: SceneLights, *, speak=None, speech_status=None):
        self.lights = lights
        self.speak = speak
        self.speech_status = speech_status
        self.narration = False
        self.lights_active = False
        self.voice_state = "off"
        self.last_announcement = 0
        self.last_scene = None

    async def apply(self, public, current):
        # A slow or unavailable lamp must not silence the current host prompt.
        # Both ports still belong to this effect generation and finish cleanup
        # before the service admits output for the next generation.
        tasks = [asyncio.create_task(self._lighting(public, current)),
                 asyncio.create_task(self._narrate(public, current))]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done() and not task.cancelling():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _lighting(self, public, current):
        try:
            await self._scene(public, current)
        except (ValueError, OSError, TimeoutError, httpx.HTTPError):
            self.lights.recovery = True

    async def _scene(self, public, current):
        if not current():
            return
        if public["phase"] in {"finished", "lobby"}:
            if public["phase"] == "finished" and self.lights_active:
                await self.lights.restore(current=current)
        elif not public["paused"]:
            night = public["phase"] in {"guard", "wolves", "seer", "witch"}
            if self.lights_active and self.last_scene != night and current():
                self.last_scene = None
                await self.lights.scene(night, public["revision"], current)
                if current():
                    self.last_scene = night

    async def _narrate(self, public, current):
        latest = public.get('host_script') or public["announcements"][-1]
        if not self.narration or latest["id"] <= self.last_announcement:
            return
        if not current() or public["paused"]:
            return
        status = self.speech_status() if self.speech_status else {}
        if status.get("quiet") is not False or status.get("configured") is not True:
            self.voice_state = "quiet" if status.get("quiet") else "unavailable"
            return
        if self.speak is None or not current():
            return
        # Only an attempted output is consumed. Pausing before the first send
        # leaves the current prompt available on resume; an uncertain send is
        # never replayed automatically.
        self.last_announcement = latest["id"]
        self.voice_state = "speaking"
        try:
            async with asyncio.timeout(25):
                receipt = await self.speak(latest["text"], current=current)
            self.voice_state = ("requested" if receipt.get("status") in {"accepted", "playing", "sent"}
                                else "quiet" if receipt.get("status") in {"paused", "muted"}
                                else "unconfirmed" if receipt.get("status") == "unconfirmed" else "unavailable")
        except asyncio.CancelledError:
            # An already issued output cannot be declared paused or replayed.
            self.voice_state = 'unconfirmed'
            raise
        except (ValueError, OSError, TimeoutError, KeyError):
            self.voice_state = "unavailable"

    def status(self):
        return dict(narration=self.narration, voice=self.voice_state,
                    physical_audio_verified=False, lights=self.lights.status())
