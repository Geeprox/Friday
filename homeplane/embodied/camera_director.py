"""Friday's camera motion policy; one bounded command, then fresh visual feedback.

A valid stream can still show the physical privacy cover. Uncovering therefore
uses local tilt impulses and image evidence, not cloud power/preset acknowledgments.
"""
import cv2
import numpy as np


def visible_scene(jpeg):
    image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_GRAYSCALE)
    if image is None:
        return False
    # Ignore the timestamp overlay: it is sharp even behind the privacy cover.
    h, w = image.shape
    crop = image[int(h*.15):int(h*.95), int(w*.05):int(w*.95)]
    crop = cv2.resize(crop, (320, 180))
    corners = cv2.goodFeaturesToTrack(crop, 120, .02, 10)
    if corners is None or len(corners) < 20 or cv2.Laplacian(crop, cv2.CV_64F).var() < 12:
        return False
    # A sharp door edge or gym rack in one corner is not a usable room view.
    # Fine spatial coverage keeps a narrow clear strip from qualifying several
    # coarse tiles. Require detail spread over more than half the view.
    cells = {(min(7,int(x/40)),min(5,int(y/30))) for [[x,y]] in corners}
    return len(cells) >= 26


class CameraDirector:
    def __init__(self):
        self.phase = 'waiting_video'
        self.last_move = -100.0
        self.last_person = None
        self.target = None
        self.target_session = None
        self.target_seen = -100.0
        self.uncover_steps = 0
        self.visible_samples = 0
        self.clear = False
        self.scan_direction = 'right'
        self.scan_steps = 0
        self.last_focus = None
        self.last_sample = None
        self.failed_moves = 0
        self.target_identity = None
        self.obscured_since = None
        self.obscured_samples = 0
        self.recovery_steps = 0
        self.recovery_direction = 'left'
        self.last_direction = None
        self.pending_direction = None
        self.position = (0, 0)
        self.blocked_positions = set()
        self.clear_since = None

    def _aim(self, direction):
        self.pending_direction = direction
        return direction

    def _next_position(self, direction):
        x,y = self.position
        return (x+(direction=='right')-(direction=='left'), y+(direction=='up')-(direction=='down'))

    def hold(self):
        # Manual positioning changes the relative origin. Do not reuse old angles.
        self.position = (0,0)
        self.blocked_positions.clear()
        self.pending_direction = None
        self.recovery_steps = 0
        self.obscured_since = None
        self.obscured_samples = 0
        self.phase = 'held'

    @staticmethod
    def identity(person):
        identity = person.get('identity') or {}
        return identity.get('person_id') if identity.get('state') in {'matched', 'tracked'} else None

    def choose(self, sample, now, *, focus=None, occupied_elsewhere=False):
        if self.failed_moves >= 3:
            self.phase = 'motor_unavailable'
            return None
        if (sample.get('status') != 'live' or not 0 <= now-sample.get('_local_received', -100) <= 1.5):
            self.phase = 'waiting_video'
            return None
        if now-self.last_move < 2:
            self.phase = 'settling'
            return None
        stamp = sample.get('_local_received')
        if stamp == self.last_sample:
            return None
        self.last_sample = stamp
        visible = sample.get('scene_visible') is True
        self.visible_samples = self.visible_samples+1 if visible else 0
        if not visible:
            self.clear_since = None
            self.obscured_samples += 1
            if self.obscured_since is None:self.obscured_since = now
            self.phase = 'view_obscured'
            if self.obscured_samples < 3 or now-self.obscured_since < 1:
                return None
            self.blocked_positions.add(self.position)
            if len(self.blocked_positions)>64:self.blocked_positions.pop()
            if self.recovery_steps >= 20:
                return None
            if not self.clear:
                # Two opening impulses, then pan out of a wall-facing angle.
                # If still covered after a bounded sweep, try two further tilts.
                step=self.recovery_steps
                direction='up' if step<2 or 8<=step<10 else ('left' if step<8 else 'right')
                self.phase='uncovering' if direction=='up' else 'recovering_view'
            else:
                if self.recovery_steps == 0:
                    self.recovery_direction='left' if self.last_direction=='right' else 'right'
                direction=self.recovery_direction
                self.phase='recovering_view'
            return self._aim(direction)
        self.obscured_samples = 0
        self.obscured_since = None
        if self.visible_samples < 2:
            self.phase='checking_view'
            return None
        self.clear = True
        self.blocked_positions.discard(self.position)
        if self.clear_since is None:self.clear_since=now
        if now-self.clear_since >= 20:self.recovery_steps=0
        people = sample.get('people', [])
        ids = {p['id'] for p in people}
        session = sample.get('session_id')
        if session != self.target_session:
            self.target = None
            self.target_identity = None
            self.target_session = session
        if focus and focus.get('track') in ids and 0 <= now-focus.get('at', -100) <= 10:
            if focus.get('at') != self.last_focus:
                self.target = focus['track']; self.last_focus = focus['at']
                self.target_identity = None
        if self.target is None and len(people) == 1:
            self.target = people[0]['id']
        target = next((p for p in people if p['id'] == self.target), None)
        if (target and self.target_identity and self.identity(target)
                and self.identity(target) != self.target_identity):
            target = None
        if target is None and self.target_identity:
            matches = [p for p in people if self.identity(p) == self.target_identity]
            if len(matches) == 1:
                target = matches[0]
                self.target = target['id']
        if target is None and len(people) == 1 and not self.target_identity:
            # Reframe a sole visible body after a pan. This is NOT a person-ID
            # match; only the identity pipeline may attach a household identity.
            target = people[0]
            self.target = target['id']
        if people:
            self.last_person = now
            if target is None:
                self.phase = 'target_ambiguous'
                return None
            self.target_seen = now
            self.target_identity = self.target_identity or self.identity(target)
            box = target['box']; error = box['x']+box['w']/2-.5
            self.phase = 'following'
            # Allow fresh post-pan face evidence to accumulate before another
            # impulse discards it. A matched identity may release this wait;
            # an unrecognizable target never stalls following indefinitely.
            if not self.identity(target) and now-self.last_move < 4:
                return None
            return self._aim('right' if error > 0 else 'left') if abs(error) > .20 else None
        if self.last_person is None:
            self.last_person = now
        if now-self.target_seen > 8:
            self.target = None
            self.target_identity = None
        if occupied_elsewhere or now-self.last_person < 15:
            self.phase = 'observing'
            return None
        if now-self.last_move < 12:
            self.phase = 'observing'
            return None
        self.phase = 'patrolling'
        if self._next_position(self.scan_direction) in self.blocked_positions:
            self.scan_direction='left' if self.scan_direction=='right' else 'right'
        if self._next_position(self.scan_direction) in self.blocked_positions:
            self.phase='observing'
            return None
        return self._aim(self.scan_direction)

    def sent(self, now, direction=None):
        self.last_move = now
        self.failed_moves = 0
        direction=direction or self.pending_direction
        if direction in {'up','down','left','right'}:
            self.position=self._next_position(direction)
            self.last_direction=direction
        self.pending_direction=None
        if self.phase in {'uncovering','recovering_view'}:
            self.recovery_steps+=1
        # Every impulse requires a new stable multi-frame view, never the frame
        # that caused that impulse. No queued or timed blind motor sequence.
        self.visible_samples=0
        self.obscured_samples=0
        self.obscured_since=None
        if self.phase == 'uncovering':
            self.uncover_steps += 1
        elif self.phase == 'patrolling':
            self.scan_steps += 1
            if self.scan_steps >= 12:
                self.scan_direction = 'left' if self.scan_direction == 'right' else 'right'
                self.scan_steps = 0

    def failed(self, now):
        self.last_move = now
        self.failed_moves += 1
        self.phase = 'motor_unavailable'

    def public(self):
        reason = {'waiting_video':'等待画面', 'settling':'镜头稳定中',
                  'checking_view':'确认视野', 'uncovering':'镜头唤醒中',
                  'recovering_view':'调整视野',
                  'view_obscured':'视野受阻', 'motor_unavailable':'云台待恢复',
                  'following':'跟随人物', 'target_ambiguous':'等待确认目标',
                  'patrolling':'环视房间', 'observing':'静候互动', 'held':'停留当前视角'}[self.phase]
        return dict(owner='friday', phase=self.phase, reason=reason, native_tracking=False,
                    native_patrol=False, view_usable=self.clear and self.phase in {
                        'following','target_ambiguous','patrolling','observing'},
                    uncover_steps=self.uncover_steps, recovery_steps=self.recovery_steps,
                    avoided_angles=len(self.blocked_positions), target_locked=self.phase == 'following',
                    physical_motion_verified=False)
