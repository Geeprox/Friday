"""One selected cover with capabilities derived from its imported MIoT spec."""

import asyncio
import collections
import json
import math
import secrets
import time

from homeplane.embodied.paths import PRIVATE

CURTAIN_DID = "selected_cover"


class Curtain:
    def __init__(self):
        self.did = CURTAIN_DID
        self.api = None
        self.monitor = None
        self.busy = False
        self.seen = collections.deque(maxlen=64)
        self.last_write = -100.0
        self.state = {
            "name": "窗帘",
            "model": None,
            "transport": "MiOT HTTP",
            "position": None,
            "target": None,
            "fault": None,
            "status": "connecting",
            "last_action": None,
        }

    async def connect(self):
        from miot.cloud import MIoTHttpClient
        from miot.types import MIoTGetPropertyParam, MIoTSetPropertyParam
        from .miot_spec import cover_binding

        binding = cover_binding()
        self.binding = binding
        self.get_parameter, self.set_parameter = MIoTGetPropertyParam, MIoTSetPropertyParam
        self.state.update(name=binding['name'], model=binding.get('model'))
        self.did = str(binding["did"])
        from .miot_session import MiSession
        token = (await MiSession(PRIVATE).current())["access_token"]
        self.api = MIoTHttpClient(cloud_server=binding.get('region', 'cn'), access_token=token)
        await self.read()

    async def close(self):
        if self.monitor:
            self.monitor.cancel()
            await asyncio.gather(self.monitor, return_exceptions=True)
            self.monitor = None
        if self.api:
            await self.api.deinit_async()
            self.api = None

    async def read(self):
        if not self.api:
            raise RuntimeError("curtain_not_connected")
        rows = await asyncio.wait_for(
            self.api.get_props_async([self.get_parameter(did=self.did, siid=s, piid=p)
                                     for s, p in (self.binding['refs'][name] for name in
                                                  ('current-position', 'target-position', 'fault'))]),
            8,
        )
        values = {(x['siid'], x['piid']): x['value'] for x in rows
                  if x.get('code') == 0 and x.get('did') == self.did}
        refs = self.binding['refs']
        if any(refs[name] not in values for name in ('current-position', 'target-position', 'fault')):
            raise RuntimeError("curtain_incomplete_readback")
        if (any(type(values[refs[name]]) is not int or not 0 <= values[refs[name]] <= 100
                for name in ('current-position', 'target-position'))
                or type(values[refs['fault']]) is not int or values[refs['fault']] < 0):
            raise RuntimeError('curtain_invalid_readback')
        self.state.update(position=values[refs['current-position']], target=values[refs['target-position']],
                          fault=values[refs['fault']], observed_at=time.time())
        if not self.busy and (not self.monitor or self.monitor.done()):
            self.state["status"] = "ready" if self.state['fault'] == 0 else 'fault'
        return dict(self.state)

    async def act(self, action, event_id):
        if action not in ("open", "close", "stop") and not (isinstance(action,str) and action.startswith("position:") and action[9:].isdigit() and 0<=int(action[9:])<=100):
            raise ValueError("curtain_action_not_allowed")
        if not isinstance(event_id, str) or not 1 <= len(event_id) <= 160:
            raise ValueError("invalid_event")
        if event_id in self.seen:
            return {"status": "duplicate"}
        if self.busy:
            return {"status": "busy"}
        self.seen.append(event_id)
        self.busy = True
        receipt = {
            "action": action,
            "event_id": event_id,
            "requested_at": time.time(),
            "acknowledged": False,
            "position_changed": False,
            "reached_target": False,
        }
        self.state.update(status="checking", last_action=receipt)
        try:
            await self.read()
            before = self.state["position"]
            target = int(action[9:]) if action.startswith("position:") else 100 if action == "open" else 0 if action=="close" else before
            if self.state["fault"] != 0:
                raise RuntimeError("curtain_fault")
            if self.binding.get("feedback_quality")!="unreliable" and action!="stop" and abs(before - target) <= 2 and self.state["target"] == target:
                receipt.update(status="already_at_target", reached_target=True)
                self.state["status"] = "ready"
                return receipt
            r = await asyncio.wait_for(
                self.api.set_prop_async(
                    self.set_parameter(did=self.did,
                        siid=self.binding['refs']['target-position' if action.startswith('position:') else 'motor-control'][0],
                        piid=self.binding['refs']['target-position' if action.startswith('position:') else 'motor-control'][1],
                        value=target if action.startswith('position:') else self.binding['commands'][action])
                ),
                8,
            )
            if r.get("code") != 0:
                raise RuntimeError("curtain_command_rejected")
            receipt["acknowledged"] = True
            receipt["status"] = "accepted"
            if self.binding.get("feedback_quality")=="unreliable":
                receipt.update(status="angle_unconfirmed",feedback_quality="unreliable",reached_target=False)
                self.state["status"]="unconfirmed"
                return receipt
            self.last_write = time.monotonic()
            self.state["status"] = "moving"
            if self.monitor:
                self.monitor.cancel()
                await asyncio.gather(self.monitor, return_exceptions=True)
            self.monitor = asyncio.create_task(self.observe_motion(receipt, before, target))
            return receipt
        except Exception as e:
            receipt.update(status="error", error_class=type(e).__name__)
            self.state["status"] = "unavailable"
            return dict(receipt)
        finally:
            self.busy = False

    async def observe_motion(self, receipt, before, target):
        try:
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                await asyncio.sleep(0.7)
                await self.read()
                receipt["position_changed"] |= self.state["position"] != before
                if abs(self.state["position"] - target) <= 2:
                    receipt.update(status="observed_at_target", reached_target=True, finished_at=time.time())
                    self.state["status"] = "ready"
                    return
            receipt["status"] = "position_readback_delayed"
            self.state["status"] = "unconfirmed"
        except asyncio.CancelledError:
            receipt["status"] = "superseded"
            raise
        except Exception as e:
            receipt.update(status="readback_unavailable", error_class=type(e).__name__)
            self.state["status"] = "unconfirmed"


class WindowGesture:
    """A paired horizontal closed-to-open movement, not a static arms-out pose."""

    def __init__(self):
        self.owner = None
        self.phase = None
        self.pending = None
        self.since = None
        self.anchor = None
        self.last = -100.0
        self.last_seen = None
        self.phase_at = None
        self.sequence = 0
        self.session = secrets.token_hex(4)
        self.public = {"pose": "unknown", "progress": 0.0, "reason": "双手先在胸前合拢，再向两侧打开"}

    def reset(self):
        self.owner = None
        self.phase = None
        self.pending = None
        self.since = None
        self.anchor = None
        self.last_seen = None
        self.phase_at = None

    def update(self, person, now):
        if not math.isfinite(now):
            self.reset()
            return None
        if self.last_seen is not None and not 0 <= now - self.last_seen <= 1.0:
            self.reset()
        self.last_seen = now
        if person is None:
            self.reset()
            return None
        if self.owner != person["id"]:
            self.reset()
            self.owner = person["id"]
            self.last_seen = now
        j = person.get("joints", {})

        def q(k):
            x = j.get(k)
            return x if x and x.get("confidence", 0) >= 0.4 else None

        ls, rs, lw, rw, lh, rh = [
            q(k) for k in ("leftShoulder", "rightShoulder", "leftWrist", "rightWrist", "leftHip", "rightHip")
        ]
        if not all((ls, rs, lw, rw, lh, rh)):
            self.pending = None
            self.since = None
            self.public = {"pose": "unknown", "progress": 0.0, "reason": "让双手、肩部与髋部清晰可见"}
            return None
        width = abs(ls["x"] - rs["x"])
        cy = (ls["y"] + rs["y"]) / 2
        cx = (ls["x"] + rs["x"]) / 2
        torso = (lh["y"] + rh["y"]) / 2 - cy
        if width < 0.025 or torso < 0.025:
            return None
        hands = sorted(((lw["x"] - cx) / width, (rw["x"] - cx) / width))
        span = hands[1] - hands[0]
        horizontal = max(abs(lw["y"] - cy), abs(rw["y"] - cy)) < torso * 0.85
        pose = (
            "open" if horizontal and span > 2.3 else "closed" if horizontal and span < 1.1 else "transition"
        )
        self.public = {"pose": pose, "span": round(span, 2), "progress": 0.0, "reason": "双臂在胸前水平开合"}
        if pose == "transition":
            self.pending = None
            self.since = None
            return None
        if pose != self.pending:
            self.pending = pose
            self.since = now
        hold = 0.18 if self.phase is None else 0.06
        self.public["progress"] = round(min(1, (now - self.since) / hold), 2)
        if now - self.since < hold:
            return None
        if self.phase is None:
            self.phase = pose
            self.anchor = hands
            self.phase_at = now
            return None
        if pose == self.phase:
            return None
        outward = hands[0] < self.anchor[0] - 0.25 and hands[1] > self.anchor[1] + 0.25
        inward = hands[0] > self.anchor[0] + 0.25 and hands[1] < self.anchor[1] - 0.25
        action = "open" if pose == "open" and outward else "close" if pose == "closed" and inward else None
        if action is None:
            return None
        if now - self.last < 0.55:
            return None
        self.phase = pose
        self.anchor = hands
        self.phase_at = now
        self.last = now
        self.sequence += 1
        self.public["reason"] = "识别到打开动作" if action == "open" else "识别到合拢动作"
        return {
            "action": action,
            "person_id": person["id"],
            "id": f"window:{self.session}:{person['id']}:{self.sequence}",
        }


class Attention:
    """Experimental binocular pupil plus head gate; unresolved eyes never qualify."""

    def __init__(self):
        self.since = {}
        self.latest = {}
        self.public = []

    def update(self, people, faces, now):
        rows = []
        live = set()
        for p in people:
            live.add(p["id"])
            nose = p.get("joints", {}).get("nose")
            best = None
            if nose:
                matches = [
                    f
                    for f in faces
                    if f["box"]["x"] - 0.02 <= nose["x"] <= f["box"]["x"] + f["box"]["w"] + 0.02
                    and f["box"]["y"] - 0.02 <= nose["y"] <= f["box"]["y"] + f["box"]["h"] + 0.02
                ]
                if matches:
                    best = max(matches, key=lambda f: f.get("confidence", 0))
            fresh = bool(best and best.get("sample_age_ms", 9999) < 400)
            facing = bool(fresh and best.get("facing_candidate"))
            clear = bool(fresh and best.get("eyes_clear"))
            gazing = bool(facing and clear and best.get("gaze_candidate"))
            if gazing:
                self.since.setdefault(p["id"], now)
            else:
                self.since.pop(p["id"], None)
            stable = gazing and now - self.since[p["id"]] >= 0.4
            reason = (
                "gaze_candidate"
                if gazing
                else "eyes_not_resolved"
                if facing and not clear
                else "eyes_away"
                if facing
                else "head_not_toward"
                if fresh
                else "face_unavailable"
            )
            row = {
                "person_id": p["id"],
                "state": "gaze_candidate" if stable else "checking" if gazing else "unknown",
                "qualified": stable,
                "head_facing": facing,
                "eyes_clear": clear,
                "reason": reason,
                "method": "head_and_pupil_geometry_experimental",
                "precise_eye_gaze": False,
            }
            if best:
                row.update(
                    yaw=best.get("yaw"),
                    pitch=best.get("pitch"),
                    face_pixels=best.get("face_pixels"),
                    eyes=best.get("eyes", []),
                    gaze_source_size=best.get("gaze_source_size"),
                )
            rows.append(row)
        self.since = {k: v for k, v in self.since.items() if k in live}
        self.public = rows
        self.latest = {r["person_id"]: r for r in rows}
        return rows

    def candidate(self, preferred=None):
        qualified = [r["person_id"] for r in self.public if r["qualified"]]
        if len(qualified) != 1:
            return None
        return qualified[0] if preferred is None or qualified[0] == preferred else None
