"""Friday's camera motion policy; one bounded command, then fresh visual feedback.

A valid stream can still show the physical privacy cover. Uncovering therefore
uses local tilt impulses and image evidence, not cloud power/preset acknowledgments.
"""
import cv2
import math
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
    REVIEW_HOLD_SECONDS = 30
    REVIEW_INTERVAL_SECONDS = 60

    def __init__(self):
        self.phase = 'waiting_video'
        self.last_move = -100.0
        self.last_person = None
        self.target = None
        self.target_session = None
        self.source_session = None
        self.target_seen = -100.0
        self.uncover_steps = 0
        self.visible_samples = 0
        self.clear = False
        self.scan_direction = 'right'
        self.scan_steps = 0
        self.last_focus = None
        self.last_sample = None
        self.failed_moves = 0
        self.retry_at = 0.0
        self.target_identity = None
        self.obscured_since = None
        self.obscured_samples = 0
        self.recovery_steps = 0
        self.recovery_direction = 'left'
        self.last_direction = None
        self.pending_direction = None
        self.pending_decision = None
        self.position = (0, 0)
        self.blocked_positions = set()
        self.clear_since = None
        self.mode = 'auto'
        self.intent = None
        self.survey_attempts = 0
        self.game_hold = False
        self.review_scope = None
        self.review_until = self.review_retry_at = -100.0

    @staticmethod
    def _game_active(game):
        if not isinstance(game, dict):
            return False
        return (game.get('mode') in {'ski', 'tennis', 'quest', 'hiit'}
                or game.get('mode') is None and game.get('status') in {'playing', 'calibrating'})

    @staticmethod
    def _game_window(game):
        if not isinstance(game, dict) or 'control_context' not in game:
            return None
        return tuple(game.get(key) for key in ('control_context', 'control_revision', 'round_id'))

    def apply_mode(self, mode, revision):
        """A survey is a finite command window, never a claim of full room coverage."""
        if mode not in {'auto', 'pets', 'hold', 'survey'}:
            raise ValueError('invalid_camera_mode')
        intent = (mode, revision)
        if intent != self.intent:
            self.review_scope = None
            if self.intent or self.mode != mode:
                self.target = None
                self.target_identity = None
                self.target_session = None
                self.last_focus = None
                self.target_seen = -100.0
                if (self.mode == 'pets') != (mode == 'pets'):
                    self.last_person = None
                self.pending_decision = None
                self.pending_direction = None
            self.intent = intent
            self.mode = mode
            self.survey_attempts = 0
            if mode == 'hold':
                self.hold()

    def _aim(self, direction, sample, now):
        if self.mode == 'survey':
            self.survey_attempts += 1  # Reserve before dispatch, including unknown/failed outcomes.
        if self.failed_moves >= 3:
            # Reserve a single probe before dispatch, even if its outcome is
            # unknown. Recovery always recomputes from new visual evidence.
            self.retry_at = now + 60
        self.pending_direction = direction
        self.pending_decision = dict(direction=direction, phase=self.phase, intent=self.intent,
            session=sample.get('session_id'), stamp=sample['_local_received'],
            target=self.target, identity=self.target_identity, game_window=self._game_window(sample.get('game')))
        return direction

    @staticmethod
    def _fresh(sample, now):
        return (sample.get('status') == 'live' and sample.get('tracking_current') is not False
                and 0 <= now-sample.get('_local_received', -100) <= 1.5)

    def _pets_current(self, sample):
        return (sample.get('animals_current') is True and self.intent is not None
                and sample.get('animals_intent') == self.intent[1]
                and isinstance(sample.get('animals'), list))

    def _subjects(self, sample):
        return sample.get('animals', []) if self.mode == 'pets' else sample.get('people', [])

    @staticmethod
    def _follow_direction(target):
        box = target['box']
        error = box['x']+box['w']/2-.5
        return ('right' if error > 0 else 'left') if abs(error) > .20 else None

    @staticmethod
    def _current_focus(sample, now, focus):
        session = sample.get('session_id')
        if (type(focus) is not dict or type(session) is not str or not session
                or focus.get('session_id') != session):
            return None
        at, track = focus.get('at'), focus.get('track')
        if (type(at) not in {int, float} or not math.isfinite(at) or not 0 <= now-at <= 10
                or type(track) not in {int, str}):
            return None
        matches = [p for p in sample.get('people', []) if p.get('id') == track]
        return focus if len(matches) == 1 else None

    def decision_current(self, sample, now, direction, *, focus=None, occupied_elsewhere=False, review_pending=False):
        """Recheck the pending purpose without consuming evidence or survey budget."""
        pending = self.pending_decision
        if (not pending or direction != self.pending_direction or self.mode == 'hold'
                or self._game_active(sample.get('game'))
                or pending.get('game_window') != self._game_window(sample.get('game'))
                or pending['intent'] != self.intent or not self._fresh(sample, now)
                or sample.get('session_id') != pending['session']
                or sample['_local_received'] < pending['stamp']
                or sample['_local_received'] <= self.last_move):
            return False
        if self._review_holds(sample, now, review_pending):
            return False
        if self.mode == 'pets' and not self._pets_current(sample):
            return False
        if pending['phase'] in {'uncovering', 'recovering_view'}:
            return sample.get('scene_visible') is False
        if sample.get('scene_visible') is not True:
            return False
        people = self._subjects(sample)
        if pending['phase'] == 'patrolling':
            return not people and not occupied_elsewhere
        if pending['phase'] != 'following':
            return False
        focus = self._current_focus(sample, now, focus)
        if (self.mode != 'pets' and focus
                and focus.get('track') != pending['target']):
            return False
        matches = [p for p in people if p['id'] == pending['target']]
        if not matches and pending['identity']:
            matches = [p for p in people if self.identity(p) == pending['identity']]
        if len(matches) != 1:
            return False
        target = matches[0]
        if pending['identity'] and self.identity(target) and self.identity(target) != pending['identity']:
            return False
        return self._follow_direction(target) == direction

    def _next_position(self, direction):
        x,y = self.position
        return (x+(direction=='right')-(direction=='left'), y+(direction=='up')-(direction=='down'))

    def hold(self):
        # Manual positioning changes the relative origin. Do not reuse old angles.
        self.position = (0,0)
        self.blocked_positions.clear()
        self.pending_direction = None
        self.pending_decision = None
        self.visible_samples = 0
        self.recovery_steps = 0
        self.obscured_since = None
        self.obscured_samples = 0
        self.phase = 'held'
        self.review_scope = None

    def _review_holds(self, sample, now, pending, *, reserve=False):
        if (pending is not True or self.mode not in {'auto', 'survey'}
                or not self._fresh(sample, now) or sample.get('scene_visible') is not True):
            return False
        scope = (self.intent, sample.get('session_id'))
        if self.review_scope == scope:
            if now < self.review_until:
                return True
            if now < self.review_retry_at:
                return False
        if reserve:
            self.review_scope = scope
            self.review_until = now + self.REVIEW_HOLD_SECONDS
            self.review_retry_at = now + self.REVIEW_INTERVAL_SECONDS
        return True

    @staticmethod
    def identity(person):
        identity = person.get('identity') or {}
        return identity.get('person_id') if identity.get('state') in {'matched', 'tracked'} else None

    def choose(self, sample, now, *, focus=None, occupied_elsewhere=False, review_pending=False):
        if self.mode == 'hold':
            self.phase = 'held'
            return None
        if self._game_active(sample.get('game')):
            # Keep the selected camera stable for the whole admitted game,
            # including waiting, pause and results. Perception still consumes
            # frames; neither clipped players nor an obscured view may pan it.
            self.game_hold = True
            self.pending_direction = self.pending_decision = None
            self.target = self.target_identity = self.target_session = None
            self.last_focus = None
            self.visible_samples = self.obscured_samples = 0
            self.clear_since = self.obscured_since = None
            fresh = self._fresh(sample, now)
            if fresh:
                self.source_session = sample.get('session_id')
                self.last_sample = (sample['_local_received'] if self.last_sample is None
                                    else max(self.last_sample, sample['_local_received']))
                self.last_person = now
            self.phase = 'game_held' if fresh else 'waiting_video'
            return None
        if self.game_hold:
            self.game_hold = False
            self.clear = False
            self.visible_samples = self.obscured_samples = 0
            self.clear_since = self.obscured_since = None
            self.phase = 'checking_view'
        if self.mode == 'survey' and self.survey_attempts >= 12:
            self.phase = 'survey_finished'
            return None
        if self.failed_moves >= 3 and now < self.retry_at:
            self.visible_samples = 0
            self.obscured_samples = 0
            self.obscured_since = None
            self.phase = 'motor_unavailable'
            return None
        if not self._fresh(sample, now) or sample['_local_received'] <= self.last_move:
            self.visible_samples = 0
            self.clear_since = None
            self.phase = 'waiting_video'
            return None
        if now-self.last_move < 2:
            self.phase = 'settling'
            return None
        if self.mode == 'pets':
            if not self._pets_current(sample):
                self.phase = 'pet_unavailable'
                return None
        session = sample.get('session_id')
        if session != self.source_session:
            self.source_session = session
            self.target = self.target_identity = self.last_focus = None
            self.last_sample = None
            self.visible_samples = self.obscured_samples = 0
            self.clear_since = self.obscured_since = None
            self.pending_direction = self.pending_decision = None
        stamp = sample.get('_local_received')
        if self.last_sample is not None and stamp <= self.last_sample:
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
            return self._aim(direction, sample, now)
        self.obscured_samples = 0
        self.obscured_since = None
        if self.visible_samples < 2:
            self.phase='checking_view'
            return None
        self.clear = True
        self.blocked_positions.discard(self.position)
        if self.clear_since is None:self.clear_since=now
        if now-self.clear_since >= 20:self.recovery_steps=0
        if self._review_holds(sample, now, review_pending, reserve=True):
            self.pending_direction = self.pending_decision = None
            self.phase = 'review_held'
            return None
        people = self._subjects(sample)
        ids = {p['id'] for p in people}
        self.target_session = session
        focus = self._current_focus(sample, now, focus)
        if self.mode != 'pets' and focus and focus.get('track') in ids:
            key = (session, focus['at'], focus['track'])
            if key != self.last_focus:
                self.target = focus['track']; self.last_focus = key
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
            self.phase = 'following'
            # Allow fresh post-pan face evidence to accumulate before another
            # impulse discards it. A matched identity may release this wait;
            # an unrecognizable target never stalls following indefinitely.
            if not self.identity(target) and now-self.last_move < 4:
                return None
            direction = self._follow_direction(target)
            return self._aim(direction, sample, now) if direction else None
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
        return self._aim(self.scan_direction, sample, now)

    def sent(self, now, direction=None):
        self.last_move = now
        self.failed_moves = 0
        self.retry_at = 0.0
        direction=direction or self.pending_direction
        if direction in {'up','down','left','right'}:
            self.position=self._next_position(direction)
            self.last_direction=direction
        self.pending_direction=None
        self.pending_decision=None
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
        if self.failed_moves >= 3:
            self.retry_at = now + 60
        self.pending_direction = None
        self.pending_decision = None
        self.visible_samples = 0
        self.obscured_samples = 0
        self.obscured_since = None
        self.phase = 'motor_unavailable'

    def public(self):
        reason = {'waiting_video':'等待画面', 'settling':'镜头稳定中',
                  'checking_view':'确认视野', 'uncovering':'镜头唤醒中',
                  'recovering_view':'调整视野',
                  'view_obscured':'视野受阻', 'motor_unavailable':'云台待恢复',
                  'following':'跟随人物', 'target_ambiguous':'等待确认目标',
                  'patrolling':'环视房间', 'observing':'静候互动', 'held':'停留当前视角',
                  'survey_finished':'本轮巡视已结束', 'pet_unavailable':'宠物识别暂不可用',
                  'game_held':'游戏中，镜头保持稳定', 'review_held':'停留观察'}[self.phase]
        if self.phase == 'following' and self.mode == 'pets':reason = '跟随宠物'
        return dict(owner='friday', phase=self.phase, reason=reason, native_tracking=False,
                    intent_revision=self.intent[1] if self.intent else None,
                    native_patrol=False, view_usable=self.clear and self.phase in {
                        'following','target_ambiguous','patrolling','observing','game_held','pet_unavailable','review_held'},
                    uncover_steps=self.uncover_steps, recovery_steps=self.recovery_steps,
                    survey_attempts=self.survey_attempts, survey_command_limit=12,
                    avoided_angles=len(self.blocked_positions), target_locked=self.phase == 'following',
                    physical_motion_verified=False)
