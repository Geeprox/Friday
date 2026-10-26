"""Camera-local operators, enrolled-person continuity and cross-view deduplication.

No SDK/models, pixels or biometric templates live here. An enrolled person is
never inferred from equal camera-local track numbers.
"""

from collections import deque
import math

from .interaction import InteractionEngine, body_gestures
from .interaction import load


class InteractionFleet:
    def __init__(self):
        self.cameras = {}
        self.operators = {}
        self.recent = {}
        self.latched = set()
        self.receipts = deque(maxlen=12)
        self.voice_camera = None
        self.voice_until = 0
        self.voice_session = None
        self.voice_order = {}
        self.voice_sequence = 0
        self.voice_now = -math.inf

    @staticmethod
    def identity(person):
        identity = person.get('identity') or {}
        return identity.get('person_id') if identity.get('state') in {'matched', 'tracked'} else None

    def session(self, camera, session):
        old = self.cameras.get(camera)
        if not old or old['session'] != session:
            if self.voice_camera == camera:
                self.voice_camera = self.voice_session = None
            self.cameras[camera] = dict(session=session, engine=InteractionEngine(camera, session),
                                        owner=None, candidate=None, since=0, suppress_ok=False,
                                        seen=-100., people=0, gestures={}, state='connecting')
        return self.cameras[camera]

    def clear(self, camera, *, voice=True):
        item = self.cameras.get(camera)
        if item:
            item.update(owner=None, candidate=None, suppress_ok=False, gestures={}, people=0, state='waiting')
            item['engine'].reset()
            if voice:
                item.update(voice_enabled=False, voice_seen=-100., voice_speaking=False,
                            voice_speech_since=None, attending=False)
        if voice and self.voice_camera == camera:
            self.voice_camera = None
            self.voice_session = None

    def operator(self, camera, session, people, hands, now, *, stationary=True):
        item = self.session(camera, session)
        if not math.isfinite(now) or now < item['seen']:
            self.clear(camera)
            return None
        if not stationary or not 0 <= now-item['seen'] <= 1.5:
            self.clear(camera)
        item.update(seen=now, people=len(people), state='ready' if stationary else 'moving')
        self.operators = {p:t for p,t in self.operators.items() if 0 <= now-t < 120}
        if not stationary:
            return None
        ids = {p['id']:p for p in people}
        if len(ids) != len(people):
            # A duplicated camera-local ID cannot select or retain an owner,
            # including when a hand carries that same ambiguous ID.
            self.clear(camera)
            return None
        owner = item['owner']
        if owner not in ids:
            item['owner'] = owner = None
            # An existing registered operator can resume in another room. Two
            # matching people are ambiguous and never resolved by array order.
            matches = [p for p in people if self.identity(p) in self.operators]
            if len(matches) == 1:
                item['owner'] = owner = matches[0]['id']
                item['suppress_ok'] = True
        if owner is None:
            # A sole continuously visible body needs no hidden activation
            # gesture or enrolled face. Multiple people still need one explicit
            # OK candidate; never select by camera order or equal track IDs.
            candidates = {h.get('person_id') for h in hands if h.get('gesture') == 'ok'} & ids.keys()
            if len(people) == 1:
                candidate = people[0]['id']
            else:
                candidate = next(iter(candidates)) if len(candidates) == 1 else None
            if candidate != item['candidate'] or candidate is None:
                item.update(candidate=candidate, since=now)
            elif now-item['since'] >= .7:
                item.update(owner=candidate, suppress_ok=True, candidate=None)
                owner = candidate
        if owner is not None:
            person_id = self.identity(ids[owner])
            if person_id:
                self.operators[person_id] = now
            labels = {h.get('gesture') for h in hands if h.get('person_id') == owner}
            if 'ok' not in labels:
                item['suppress_ok'] = False
        return owner

    def update(self, camera, session, people, hands, registration, active, now):
        stationary = registration.get('status') not in {'moving', 'settling', 'lost', 'stale'}
        if not active:
            item = self.session(camera, session)
            # A game may consume this camera's gestures while its microphone
            # remains a household consumer. Actual source loss uses clear().
            self.clear(camera, voice=False)
            item.update(seen=now, people=len(people))
            return []
        owner = self.operator(camera, session, people, hands, now, stationary=stationary)
        item = self.cameras[camera]
        person = next((p for p in people if p['id'] == owner), None)
        actor = ('person', self.identity(person)) if person and self.identity(person) else ('track', camera, session, owner)
        visible = body_gestures(person) if person else set()
        visible.update('hand.'+h['gesture'] for h in hands if h.get('person_id') == owner and h.get('gesture') in {'ok','yeah','open_palm'})
        item['gestures'] = {actor:visible} if person else {}
        # Only release a shared latch when every fresh view of the person has
        # released. A missing or stale view cannot keep a latch forever.
        for key in list(self.latched):
            who, binding, gesture = key
            if not any(now-c['seen'] < 1.5 and gesture in c['gestures'].get(who, set()) for c in self.cameras.values()):
                self.latched.discard(key)
        admitted_hands = [h for h in hands if not (item['suppress_ok'] and h.get('gesture') == 'ok')]
        events = item['engine'].update(people, admitted_hands, owner, registration, active, now)
        result = []
        bindings = {b.id:b for b in item['engine'].config.bindings}
        self.recent = {k:t for k,t in self.recent.items() if now-t < 10}
        for event in events:
            b = bindings[event['binding']]
            latch = (actor, b.id, b.gesture)
            key = (actor, b.target, b.action)
            if latch in self.latched or now-self.recent.get(key, -100.) < 3:
                continue
            self.recent[key] = now
            if b.gesture not in {'body.arms_open','body.arms_close','hand.point'}:
                self.latched.add(latch)
            result.append(event)
        return result

    def current(self, event, now):
        try:
            current = load()
        except (OSError, ValueError):
            return False
        binding = next((b for b in current.bindings if b.id == event['binding']), None)
        if not (current.enabled and current.revision == event.get('revision') and binding and binding.enabled
                and binding.target == event['target'] and binding.action == event['action']):
            return False
        item = self.cameras.get(event['camera'])
        return bool(item and item['session'] == event['session'] and item['owner'] == event['track']
                    and item['state'] == 'ready' and 0 <= now-event['observed_mono'] <= 1.5
                    and now-item['seen'] <= 1.5)

    def voice(self, camera, now, attending, *, speaking=False, enabled=True, people=None, session=None):
        """Arbitrate fresh microphone windows independently of callback order.

        Qualified attention outranks presence, and presence outranks empty views.
        Same-priority cameras take turns after four seconds. A current utterance
        holds its source for at most 8.5 seconds; callers supply *fresh* VAD state.
        No audio, transcript or identity inference enters this coordinator.
        """
        item = self.cameras.get(camera)
        # Observations establish sessions. A delayed audio callback must never
        # replace the new session or move the shared microphone clock backwards.
        if item is None or (session is not None and item['session'] != session):
            return False
        if not math.isfinite(now) or now < self.voice_now:
            return False
        self.voice_now = now
        count = item['people'] if people is None else people
        count = count if isinstance(count, int) and 0 <= count <= 32 else 0
        was_speaking = item.get('voice_speaking', False)
        item.update(voice_seen=now, voice_enabled=bool(enabled), voice_people=count,
                    attending=bool(enabled and attending and count), voice_speaking=bool(enabled and speaking))
        if not item['voice_speaking']:
            item['voice_speech_since'] = None
        elif self.voice_camera == camera and (not was_speaking or item.get('voice_speech_since') is None):
            item['voice_speech_since'] = now

        def fresh(value):
            return value.get('voice_enabled', False) and 0 <= now-value.get('voice_seen', -100.) <= 1.5

        def priority(value):
            return 2 if value.get('attending') else 1 if value.get('voice_people', 0) else 0

        eligible = {key:value for key,value in self.cameras.items() if fresh(value)}
        current = eligible.get(self.voice_camera)
        if current is None or current['session'] != self.voice_session:
            self.voice_camera = self.voice_session = None
            current = None
        if not eligible:
            return False
        speech_at = current.get('voice_speech_since') if current else None
        in_utterance = bool(current and current.get('voice_speaking') and speech_at is not None
                            and 0 <= now-speech_at < 8.5)
        highest = max(priority(value) for value in eligible.values())
        if not in_utterance and (current is None or now >= self.voice_until or highest > priority(current)):
            best = [key for key,value in eligible.items() if priority(value) == highest]
            selected = min(best, key=lambda key:(self.voice_order.get(key, 0), key))
            self.voice_sequence += 1
            self.voice_order[selected] = self.voice_sequence
            self.voice_camera = selected
            self.voice_session = eligible[selected]['session']
            self.voice_until = now + 4
        return bool(enabled and self.voice_camera == camera and item['session'] == self.voice_session)

    def voice_current(self, camera, now):
        """Check a packet's microphone lease without renewing or selecting it."""
        item = self.cameras.get(camera)
        return bool(item and math.isfinite(now) and now >= self.voice_now
                    and self.voice_camera == camera and item['session'] == self.voice_session
                    and item.get('voice_enabled', False)
                    and 0 <= now-item.get('voice_seen', -100.) <= 1.5)

    def public(self, now):
        return dict(scope='all_cameras', cameras=[dict(camera=k, state=v['state'] if now-v['seen'] < 1.5 else 'waiting',
            people=v['people'] if now-v['seen'] < 1.5 else 0, operator=v['owner'] if now-v['seen'] < 1.5 else None,
            interaction=v['engine'].public) for k,v in self.cameras.items()], events=list(self.receipts))
