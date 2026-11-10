"""Bounded recognition rehearsal on existing observations; no source or device I/O."""

import secrets
import time
import math

from .practice_requests import valid_id

from .interaction import GESTURES, body_gestures
from .smart_home import WindowGesture
from .gestures import BINDABLE_HAND_LABELS
from .hiit import EXERCISE_GESTURES, PLAN

MOTION_GESTURES = {
    'body.lateral', 'body.arm_effort', 'body.swing', 'pose.horizontal', 'pose.overhead',
} | EXERCISE_GESTURES.keys()
SUPPORTED = frozenset(GESTURES) | MOTION_GESTURES | {'hand.point'}


class RecognitionPractice:
    """One explicit 90-second lease. Results contain no image, joints or identity."""

    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.lease = None
        self.observers = {}
        self.scope = secrets.token_hex(16)
        self.requests = {}

    def capabilities(self):
        return dict(scoped_requests=1, scope=self.scope)

    def request_context(self, request_id, expected_scope):
        if not valid_id(request_id) or not isinstance(expected_scope,str) or not secrets.compare_digest(expected_scope,self.scope):
            raise ValueError('practice_source_changed')
        return dict(request_id=request_id, scope=self.scope)

    def _requests(self, request_id):
        now = self.clock()
        self.requests = {key:value for key,value in self.requests.items() if value['expires'] > now}
        if request_id not in self.requests and len(self.requests) >= 128:
            raise ValueError('practice_request_capacity')
        return self.requests.get(request_id)

    def begin_request(self, gesture, cameras, request_id, expected_scope, not_after, lease_deadline):
        context = self.request_context(request_id,expected_scope)
        now = self.clock()
        if (type(not_after) not in {int,float} or type(lease_deadline) not in {int,float}
                or not math.isfinite(not_after) or not math.isfinite(lease_deadline)
                or not now < not_after <= now+10 or not now < lease_deadline <= now+90):
            raise ValueError('practice_request_expired')
        record = self._requests(request_id)
        signature = (gesture,tuple(sorted(set(cameras))))
        if record is not None:
            if record['signature'] not in {None,signature}:
                raise ValueError('practice_request_conflict')
            return self.status_request(request_id,expected_scope)
        self.requests[request_id] = dict(signature=signature,expires=now+120)
        try:
            result = self.begin(gesture,cameras)
        except ValueError:
            return dict(phase='blocked',reason='practice_active',**context)
        self.lease.update(request_id=request_id,deadline=lease_deadline)
        return self.status(result['token']) | context

    def status_request(self,request_id,expected_scope):
        context = self.request_context(request_id,expected_scope)
        lease = self.active()
        if lease is not None and lease.get('request_id') == request_id:
            return self.status(lease['token']) | context
        return dict(phase='ended',remaining=0,progress=0.0,**context)

    def stop_request(self,request_id,expected_scope):
        context = self.request_context(request_id,expected_scope)
        record = self._requests(request_id)
        if record is None:
            self.requests[request_id] = dict(signature=None,expires=self.clock()+120)
        lease = self.active()
        if lease is not None and lease.get('request_id') == request_id:
            self.stop(lease['token'])
        return dict(phase='ended',remaining=0,progress=0.0,**context)

    def active(self):
        if self.lease and self.clock() >= self.lease['deadline']:
            self.lease = None
            self.observers.clear()
        return self.lease

    def begin(self, gesture, cameras):
        if gesture not in SUPPORTED or not cameras:
            raise ValueError('practice_input_unavailable')
        if self.active():
            raise ValueError('practice_already_active')
        self.observers.clear()
        self.lease = dict(token=secrets.token_hex(16), gesture=gesture, cameras=frozenset(cameras),
                          deadline=self.clock() + 90, phase='waiting', progress=0.0,
                          camera=None, event=None)
        return self.status(self.lease['token'])

    def status(self, token):
        lease = self.active()
        if not lease or not secrets.compare_digest(token, lease['token']):
            return dict(phase='ended', remaining=0, progress=0.0)
        if lease['phase'] != 'success':
            fresh = [(camera, item) for camera, item in self.observers.items()
                     if 0 <= self.clock() - item['sample_at'] <= 1.5]
            if not fresh:
                lease.update(phase='waiting', progress=0.0, camera=None)
            else:
                camera, best = max(fresh, key=lambda pair: (
                    {'multiple': 0, 'seen': 1, 'recognizing': 2}.get(pair[1].get('phase'), 0),
                    pair[1].get('progress', 0)))
                lease.update(camera=camera, phase=best.get('phase', 'seen'), progress=best.get('progress', 0.0))
        return {k: lease[k] for k in ('token', 'gesture', 'phase', 'progress', 'camera', 'event')} | {
            'remaining': max(0, round(lease['deadline'] - self.clock()))}

    def stop(self, token):
        if self.active() and secrets.compare_digest(token, self.lease['token']):
            self.lease = None
            self.observers.clear()
        return dict(phase='ended', remaining=0, progress=0.0)

    def suppresses(self, camera):
        lease = self.active()
        return bool(lease and camera in lease['cameras'])

    def observe(self, camera, session, people, hands, registration, now, captured_at):
        lease = self.active()
        if not lease or camera not in lease['cameras'] or lease['phase'] == 'success':
            return
        # Replayed samples, lost sources and camera movement cannot complete a lesson.
        if (not 0 <= self.clock() - captured_at <= 1.5
                or registration.get('status') in {'moving', 'settling', 'lost', 'stale'}):
            self.observers.pop(camera, None)
            lease.update(phase='waiting', progress=0.0)
            return
        if len(people) != 1:
            self.observers.pop(camera, None)
            if people:
                self.observers[camera] = dict(key=None, sample_at=captured_at, phase='multiple', progress=0.0)
            return
        person = people[0]
        key = (session, person['id'])
        item = self.observers.get(camera)
        if not item or item['key'] != key or now - item['sample_at'] > .5:
            from .motion_games import BodyMotion
            from .hiit import HIITCircuit
            action = EXERCISE_GESTURES.get(lease['gesture'])
            circuit = (HIITCircuit(plan=(next(row for row in PLAN if row[0] == action),), require_neutral=True)
                       if action else None)
            item = dict(key=key, sample_at=-100., since=None, arms=WindowGesture(),
                        motion=BodyMotion(), circuit=circuit)
            self.observers[camera] = item
        if captured_at <= item['sample_at']:
            return
        dt = min(.15, max(.01, now - item['sample_at']))
        item['sample_at'] = captured_at
        gesture = lease['gesture']
        labels = body_gestures(person)
        labels.update('hand.' + h['gesture'] for h in hands
                      if h.get('person_id') == person['id'] and h.get('gesture') in BINDABLE_HAND_LABELS)
        labels.update('hand.point' for h in hands if h.get('person_id') == person['id']
                      and h.get('gesture') == 'pointing' and h.get('ray'))
        matched = gesture in labels
        immediate = False
        if gesture in {'body.arms_open', 'body.arms_close'}:
            event = item['arms'].update(person, now)
            matched = bool(event and gesture == 'body.arms_' + event['action'])
            immediate = True
        elif gesture in MOTION_GESTURES:
            p = item['motion'].update(person, now)
            matched = False
            if p and p.get('observation') == 'fresh':
                if gesture.startswith('pose.'):
                    matched = p.get('pose_quality', {}).get(gesture.split('.')[1], 0) >= .6
                elif gesture == 'body.lateral':
                    matched = p.get('calibrated') and abs(p['lateral']) > .4
                elif gesture == 'body.arm_effort':
                    matched = p.get('pumps', 0) > 0 and 0 <= now - p['last_pump'] < .25
                elif gesture == 'body.swing':
                    matched = p.get('swings', 0) > 0 and 0 <= now - p['last_swing'] < .25
                else:
                    item['circuit'].update(p, dt)
                    matched = item['circuit'].total_reps > 0
                immediate = not gesture.startswith('pose.')
        lease.update(phase='seen', camera=camera, progress=0.0)
        item.update(phase='seen', progress=0.0)
        if not matched:
            item['since'] = None
            return
        if item['since'] is None:
            item['since'] = now
        progress = 1.0 if immediate else min(1.0, (now - item['since']) / .7)
        lease.update(phase='recognizing', progress=round(progress, 2))
        item.update(phase='recognizing', progress=round(progress, 2))
        if progress >= 1:
            lease.update(phase='success', event=dict(id=secrets.token_hex(8), gesture=gesture, camera=camera))
