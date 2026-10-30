"""Demo-owned preset search. Native patrol/tracking are disabled during the lease.

Video and decisions stay local. This prototype's small control messages use the
official MiOT HTTP API; an acknowledgement is never proof of physical movement.
"""

import asyncio
import base64
import fcntl
import json
import logging
import math
import os
import secrets
import stat
import tempfile
import time

from homeplane.embodied.paths import PRIVATE, camera, camera_storage

FLAGS = dict.fromkeys(('cruise', 'human_tracking', 'pet_tracking', 'human_close_up'))
logging.getLogger("miot").setLevel(logging.CRITICAL)


def atomic(path, data):
    fd, name = tempfile.mkstemp(prefix='.ptz-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:
        if os.path.exists(name):os.unlink(name)


def read_lease(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
            raise ValueError('invalid_camera_lease')
        value = json.load(stream)
    if not isinstance(value, dict):raise ValueError('invalid_camera_lease')
    return value


def alive(pid):
    try:
        if isinstance(pid, bool) or int(pid) <= 0:return False
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, ValueError, TypeError):
        return False
    except PermissionError:
        return True


class Transport:
    def __init__(self, camera_id=None):
        from miot.cloud import MIoTHttpClient
        from .miot_spec import camera_properties

        selected = camera(camera_id)
        self.refs = camera_properties(selected)
        self.did = selected["device"]["did"]
        login = json.loads((PRIVATE / "miot-session.json").read_text())
        self.api = MIoTHttpClient(cloud_server=selected.get('region', 'cn'), access_token=login["access_token"])

    async def read(self):
        from miot.types import MIoTGetPropertyParam

        result = await asyncio.wait_for(
            self.api.get_props_async(
                [MIoTGetPropertyParam(did=self.did, siid=s, piid=p) for s, p in self.refs.values()]
            ),
            18,
        )
        values = {
            name: r.get("value")
            for r in result
            for name, (s, p) in self.refs.items()
            if (s, p) == (r["siid"], r["piid"]) and r.get("code") == 0
        }
        if any(name not in values for name in self.refs):
            raise RuntimeError("incomplete_camera_readback")
        return values

    async def write(self, name, value):
        from miot.types import MIoTSetPropertyParam

        if name not in FLAGS and name not in ("active_preset", "camera_power"):
            raise ValueError("property_not_allowed")
        s, p = self.refs[name]
        result = await asyncio.wait_for(self.api.set_prop_async(
            MIoTSetPropertyParam(did=self.did, siid=s, piid=p, value=value)), 12)
        if result.get("code") != 0:
            raise RuntimeError("camera_write_code_" + str(result.get("code")))
        return {"code": 0, "property": name, "value": value}

    async def close(self):
        await self.api.deinit_async()


class CameraLease:
    def __init__(self, transport, path=None, initial_baseline=None, *, open_privacy=False, restore_native=True):
        self.io = transport
        self.open_privacy = open_privacy
        # Retain the old argument for callers, but leases always restore their
        # original flags. Permanent vendor-tracking suppression is unsupported.
        self.restore_native = True
        self.path = path if path is not None else camera_storage() / 'ptz-lease.json'
        self.initial_baseline = initial_baseline
        self.snapshot = None
        self.presets = []
        self.active = None
        self.events = []
        self.lock_fd = None

    def claim(self):
        if self.lock_fd is not None:return
        fd = os.open(self.path.with_suffix('.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):raise ValueError('invalid_camera_lock')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise RuntimeError('camera_owned_by_live_process') from None
        except BaseException:
            os.close(fd)
            raise
        self.lock_fd = fd

    def unclaim(self):
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None

    def event(self, kind, **fields):
        self.events.append(dict(at=time.time(), event=kind, **fields))
        self.events = self.events[-100:]
        if self.snapshot:
            self.snapshot["events"] = self.events
            atomic(self.path, self.snapshot)

    async def read_until(self, expected, seconds=5):
        deadline = time.monotonic() + seconds
        while True:
            result = await self.io.read()
            if all(result.get(k) == v for k, v in expected.items()):
                return result
            if time.monotonic() >= deadline:
                raise RuntimeError("camera_readback_timeout")
            await asyncio.sleep(0.4)

    async def acquire(self):
        self.claim()
        try:
            await self._acquire()
        except BaseException:
            self.unclaim()
            raise

    async def _acquire(self):
        if self.path.exists():
            prior = read_lease(self.path)
            if prior.get("status") != "restored":
                # Version 2 uses the OS lock, which survives PID reuse safely.
                if prior.get('version', 1) < 2 and alive(prior.get("pid")) and prior.get("pid") != os.getpid():
                    raise RuntimeError("camera_owned_by_live_process")
                self.snapshot = prior
                self.events = prior.get("events", [])
                if not await self._restore():
                    raise RuntimeError("previous_restore_pending")
        before = await self.io.read()
        if not self.open_privacy and before.get('camera_power') is not True:
            raise RuntimeError('privacy_shutter_closed')
        original = {k: before[k] for k in FLAGS}
        if any(type(value) is not bool for value in original.values()):
            raise RuntimeError('incomplete_camera_readback')
        if "camera_power" in before:
            original["camera_power"] = before["camera_power"]
        if self.initial_baseline and not self.path.exists():
            original.update(self.initial_baseline)
        self.snapshot = {
            "version": 2,
            "lease_id": secrets.token_hex(16),
            "owner_token": os.environ.get('FRIDAY_CAMERA_OWNER'),
            "pid": os.getpid(),
            "status": "acquiring",
            "acquired_at": time.time(),
            "before": original,
            "observed_before": {k: before[k] for k in FLAGS},
            "return_preset": before["active_preset"],
            "restore_native": True,
        }
        self.events = []
        atomic(self.path, self.snapshot)
        try:
            if before.get("camera_power") is False:
                await self.io.write("camera_power", True)
            for name in FLAGS:
                if before[name] is not False:
                    await self.io.write(name, False)
            expected = {name: False for name in FLAGS}
            if "camera_power" in before:
                expected["camera_power"] = True
            after = await self.read_until(expected)
            raw = after["presets"]
            raw = json.loads(raw) if isinstance(raw, str) else raw
            for p in raw:
                p = dict(p)
                try:
                    p["name"] = base64.b64decode(p["name"]).decode()
                except (ValueError, UnicodeError):
                    pass
                self.presets.append(p)
            self.active = after["active_preset"]
            self.snapshot["status"] = "owned"
            self.event("acquired", disabled=list(FLAGS))
        except BaseException:
            await self._restore()
            raise

    async def move(self, preset):
        if self.lock_fd is None or not self.snapshot or self.snapshot.get('status') != 'owned':
            raise RuntimeError('camera_not_owned')
        if (await self.io.read()).get('camera_power') is not True:
            raise RuntimeError('privacy_shutter_closed')
        # Existing favourite IDs only. Never modify the household's preset definitions.
        if preset not in {p["idx"] for p in self.presets}:
            raise ValueError("unknown_preset")
        began = time.monotonic()
        await self.io.write("active_preset", int(preset))
        self.event("preset_command_accepted", preset=preset)
        await self.read_until({"active_preset": preset})
        self.active = preset
        self.event("preset_acknowledged", preset=preset, latency_ms=round((time.monotonic() - began) * 1000))

    async def release(self):
        try:
            if self.snapshot and self.snapshot.get('status') != 'restored':
                self.claim()
                current = read_lease(self.path)
                if current.get('lease_id') != self.snapshot.get('lease_id'):
                    raise RuntimeError('stale_camera_lease')
                return await self._restore()
            return True
        finally:
            self.unclaim()

    async def _restore(self):
        if not self.snapshot or self.snapshot.get("status") == "restored":
            return True
        # Old in-progress leases also restore the original flags, even if an old
        # worker saved restore_native=False. Never replace the baseline on retry.
        before = self.snapshot.get('before', {})
        if any(type(before.get(name)) is not bool for name in FLAGS):
            raise ValueError('invalid_camera_lease')
        self.snapshot['restore_native'] = True
        self.snapshot["status"] = "restoring"
        atomic(self.path, self.snapshot)
        errors = []
        returned_preset = False
        try:
            current = await self.io.read()
        except Exception:
            self.snapshot.update(status='restore_pending', restore_errors=['camera_readback_unavailable'])
            self.event('restore', ok=False)
            return False
        # Stop automatic movement before returning the view; restore native patrol last.
        try:
            if current.get("camera_power") is True:
                await self.io.write("cruise", False)
                await self.io.write("human_tracking", False)
                await self.io.write("pet_tracking", False)
                current = await self.io.read()
            if current.get("camera_power") is True:
                await self.io.write("active_preset", self.snapshot["return_preset"])
                returned_preset = True
        except Exception as e:
            errors.append(type(e).__name__)
        for name in ("human_close_up", "pet_tracking", "human_tracking", "cruise"):
            try:
                await self.io.write(name, before[name])
            except Exception as e:
                errors.append(name + ":" + type(e).__name__)
        expected = {name: before[name] for name in FLAGS}
        if returned_preset:expected['active_preset'] = self.snapshot['return_preset']
        if 'camera_power' in before:
            try:
                current = await self.io.read()
                if current.get("camera_power") is False and before['camera_power'] is True:
                    self.snapshot["manual_preview_close_preserved"] = True
                # Only undo a temporary explicit opening. Never restore power-on
                # or require it in readback after a newer manual close.
                elif before['camera_power'] is False:
                    intent_path=self.path.parent/'power-intent.json'
                    intent=read_lease(intent_path) if intent_path.exists() else {}
                    if (intent.get('enabled') is True and type(intent.get('at')) in (int,float)
                            and intent['at']>self.snapshot.get('acquired_at',0)):
                        self.snapshot['newer_explicit_open_preserved']=True
                        expected.pop('camera_power',None)
                        preserve_open=True
                    else:preserve_open=False
                    if not preserve_open:
                        await self.io.write("camera_power", False)
                        expected['camera_power'] = False
            except Exception as e:
                errors.append("camera_power:" + type(e).__name__)
        try:
            result = await self.read_until(expected)
            mismatch = [k for k, v in expected.items() if result[k] != v]
            if mismatch:
                errors.extend(mismatch)
            self.snapshot["restored_readback"] = {k: result[k] for k in expected}
        except Exception as e:
            errors.append("readback:" + type(e).__name__)
        self.snapshot["status"] = "restore_pending" if errors else "restored"
        self.snapshot["restore_errors"] = errors
        self.event("restore", ok=not errors)
        return not errors


class SearchPlanner:
    """No native tracking: local detections pause or advance our own view search."""

    def __init__(self, presets, now, current):
        self.ids = [p["idx"] for p in presets]
        self.current = current
        self.last_move = now
        self.last_person = now
        self.person_since = None
        self.mode = "auto"
        self.phase = "observing"
        self.reason = "启动观察"
        self.visited = set()
        self.views = {}
        self.edge_since = None

    def choose(self, state, now, reference=None):
        if self.mode == "hold":
            self.phase = "held"
            self.reason = "手动停留"
            return None
        if state.get("status") != "live" or now - state.get("_local_received", now) > 1.5:
            self.phase = "waiting_video"
            self.reason = "等待新鲜画面"
            return None
        reg = (state.get("room") or {}).get("registration") or {}
        if reg.get("status") in ("moving", "settling") or now - self.last_move < 3:
            self.phase = "settling"
            self.reason = "等待镜头稳定"
            return None
        if reference and reg.get("status") == "locked":
            self.views[self.current] = reference
        people = state.get("people", [])
        if not people:
            scene = state.get("scene") or {}
            if time.time() - scene.get("observed_at", 0) < 1.2:
                people = [
                    o
                    for o in scene.get("objects", [])
                    if o.get("class") == "person" and o.get("confidence", 0) >= 0.5
                ]
        if any(len(h.get("joints", {})) >= 12 for h in state.get("hands", [])):
            self.last_person = now
            self.phase = "observing"
            self.reason = "手部可见，保持识别"
            return None
        if people:
            self.last_person = now
            self.person_since = self.person_since or now
            self.phase = "observing"
            self.reason = "玩家可见，保持识别"
            if state.get("hands") or len(people) > 1:
                self.edge_since = None
                return None
            p = people[0]
            b = p["box"]
            x = b["x"] + b["w"] / 2
            if 0.12 < x < 0.88:
                self.edge_since = None
                return None
            self.edge_since = self.edge_since or now
            if now - self.edge_since < 2 or now - self.last_move < 6:
                return None
            # Move only to another *observed* view that centers this same reference ray.
            if reference and reference.get("target"):
                target = reference["target"]
                here = math.dist(target, reference["center"])
                ranked = [
                    (math.dist(target, v["center"]), k) for k, v in self.views.items() if k != self.current
                ]
                distance, k = min(ranked, default=(math.inf, None))
                if k is not None and distance < here * 0.65:
                    self.phase = "following"
                    self.reason = "切换到更居中的已知视角"
                    return k
            self.reason = "玩家临近边缘，尚无更佳已知视角"
            return None
        self.person_since = None
        self.edge_since = None
        if now - self.last_person < 8 or now - self.last_move < 12:
            self.phase = "observing"
            self.reason = "短暂丢失，保留当前视角"
            return None
        self.phase = "searching"
        self.reason = "无人可见，分区搜索"
        if self.mode == "survey" and len(self.visited) >= len(self.ids):
            self.mode = "hold"
            self.phase = "held"
            self.reason = "一轮分区搜索完成"
            return None
        index = (self.ids.index(self.current) + 1) if self.current in self.ids else 0
        return self.ids[index % len(self.ids)] if self.ids else None

    def moved(self, k, now):
        self.current = k
        self.last_move = now
        self.visited.add(k)

    def public(self, presets):
        return {
            "owner": "friday",
            "mode": self.mode,
            "phase": self.phase,
            "reason": self.reason,
            "preset": self.current,
            "preset_name": next((p["name"] for p in presets if p["idx"] == self.current), "当前视角"),
            "presets": [{"id": p["idx"], "name": p["name"]} for p in presets],
            "native_tracking": False,
            "native_patrol": False,
            "control_transport": "MiOT HTTP",
            "known_views": len(self.views),
        }


class LocalFollower:
    """Image-feedback controller. One bounded impulse, then wait for settling.

    C700 impulses are relatively large, so use a wide dead band and no backlog.
    Horizontal-only following preserves the calibrated pitch and floor visibility.
    """

    def __init__(self):
        self.last = -10.0
        self.edge_since = None
        self.last_direction = None

    def choose(self, state, now):
        if state.get("status") != "live" or now - state.get("_local_received", now) > 1.5:
            return None
        if (state.get("room") or {}).get("registration", {}).get("status") != "locked":
            self.edge_since = None
            return None
        people = state.get("people", [])
        if not people:
            self.edge_since = None
            return None
        left = min(p["box"]["x"] for p in people)
        right = max(p["box"]["x"] + p["box"]["w"] for p in people)
        # Never center one person at the expense of clipping another visible player.
        if len(people) > 1 and right - left > 0.65:
            self.edge_since = None
            return None
        if (
            (state.get("game") or {}).get("status") in ("playing", "calibrating")
            and left > 0.04
            and right < 0.96
        ):
            self.edge_since = None
            return None
        error = (left + right) / 2 - 0.5
        if abs(error) < 0.22:
            self.edge_since = None
            return None
        direction = "right" if error > 0 else "left"
        if direction != self.last_direction:
            self.edge_since = now
        self.last_direction = direction
        self.edge_since = self.edge_since or now
        if now - self.edge_since < 0.5 or now - self.last < 2:
            return None
        return direction

    def sent(self, now):
        self.last = now
        self.edge_since = None


async def restore_camera(camera_id=None, *, path=None, owner_token=None, transport_factory=Transport):
    path = path if path is not None else camera_storage(camera_id) / 'ptz-lease.json'
    if not path.exists():return True
    lease = CameraLease(None, path)
    lease.claim()
    io = None
    try:
        data = read_lease(path)
        if data.get('status') == 'restored' or owner_token is not None and data.get('owner_token') != owner_token:
            return True
        if data.get('version', 1) < 2 and alive(data.get('pid')):
            raise RuntimeError('camera_owner_still_running')
        io = transport_factory(camera_id)
        lease.io, lease.snapshot, lease.events = io, data, data.get('events', [])
        return await lease._restore()
    finally:
        try:
            if io is not None:await asyncio.wait_for(io.close(), 5)
        finally:lease.unclaim()


async def restore_only():
    ok = await restore_camera()
    print(json.dumps({'camera_settings_restored': ok}))
    if not ok:raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(restore_only())
