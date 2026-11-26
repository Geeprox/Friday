"""Friday's camera motion policy; one bounded command, then fresh visual feedback.

A valid stream can still show the physical privacy cover. Uncovering therefore
uses local tilt impulses and image evidence, not cloud power/preset acknowledgments.
"""
import cv2
import math
import numpy as np

from .camera_audio import WakeCameraCue, WakeCameraPause
from .camera_intent import CAMERA_MODES, PET_MODES, TARGET_MODES, finish_camera_survey
from .catalog import MOTION_GAME_MODES
from .pose_rules import iou


def visible_scene(jpeg):
    image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_GRAYSCALE)
    return visible_scene_luma(image)


def visible_scene_luma(image):
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


def _view_shift(before, after):
    """求新画面中心在原画面里的位移。配准参考必须来自同一个 Room 进程。"""
    if before is None or after is None or before[0] != after[0]:
        return None
    try:
        center = np.linalg.solve(before[1], np.asarray(after[1]) @ (.5, .5, 1))
    except (TypeError, ValueError, np.linalg.LinAlgError):
        return None
    if center.shape != (3,) or not np.isfinite(center).all() or abs(center[2]) < 1e-6:
        return None
    return tuple(center[:2] / center[2] - .5)


class CameraDirector:
    REVIEW_HOLD_SECONDS = 30
    REVIEW_INTERVAL_SECONDS = 60
    FALL_HOLD_SECONDS = 8
    FALL_INTERVAL_SECONDS = 20
    PATROL_CONFIRM_SECONDS = 8
    AUTO_PATROL_RETRY_SECONDS = 30
    FOLLOW_REACQUIRE_SECONDS = 8
    FOLLOW_REFERENCE_INTERVAL = 2
    TILT_DIRECTIONS = ('up', 'down')
    SURVEY_ATTEMPT_LIMIT = 12
    SURVEY_ROUTE = ('right', 'right', 'down', 'left', 'left', 'left', 'left', 'up', 'right', 'right')
    PATROL_AXES = {'right': (0, 1), 'left': (0, -1), 'down': (1, 1), 'up': (1, -1)}
    # 先抬起镜头脱离遮挡，再把俯仰降回房间方向后扫过两侧。
    UNCOVER_ROUTE = ('up', 'up') + ('left',) * 3 + ('down',) * 4 + ('left',) * 4 + ('right',) * 7
    VIEW_RECOVERY_ROUTE = ('left',) * 4 + ('up',) * 2 + ('right',) * 8 + ('down',) * 3 + ('left',) * 3
    # 在不同俯仰层横向查找。一轮步数左右相抵，避免持续向同一侧偏移。
    VIEW_RECOVERY_PROBES = (
        ('down', 'down') + ('right',) * 8,
        ('up', 'up') + ('left',) * 8,
        ('up', 'up') + ('left',) * 8,
        ('down', 'down') + ('right',) * 8,
    )
    VIEW_RECOVERY_DELAYS = (30, 60, 120)

    def __init__(self):
        self.phase = 'waiting_video'
        self.last_move = -100.0
        self.last_person = None
        self.target = None
        self.target_session = None
        self.source_session = None
        self.target_seen = -100.0
        self.selection_sample = None
        self.selection_reference = None
        self.selection_reference_attempt = -math.inf
        self.uncover_steps = 0
        self.visible_samples = 0
        self.clear = False
        self.scan_direction = 'right'
        self.scan_steps = 0
        self.last_focus = None
        self.last_sample = None
        self.tilt_observation = None
        self.tilt_recovery = None
        self.failed_moves = 0
        self.retry_at = 0.0
        self.target_identity = None
        self.auto_reconfirmed = None
        self.single_person_move = False
        self.followed_person = False
        self.reframe_started = None
        self.reframe_appearance = None
        self.reframe_candidate = None
        self.obscured_since = None
        self.obscured_samples = 0
        self.recovery_steps = 0
        self.recovery_retry_at = 0.0
        self.recovery_pan_reversed = False
        self.last_direction = None
        self.pending_direction = None
        self.pending_decision = None
        self.position = (0, 0)
        self.blocked_positions = set()
        self.clear_since = None
        self.recovery_view_stamp = None
        self.mode = 'auto'
        self.intent = None
        self.survey_attempts = 0
        self.survey_confirmed_moves = 0
        self.survey_reference = None
        self.survey_interrupted = False
        self.survey_interruption = None
        self.patrol_confirmation = None
        self.patrol_view_state = 'idle'
        self.patrol_retry_at = 0.0
        self.game_hold = False
        self.review_windows = {}
        self.sound_search_cue = None
        self.sound_view = None
        self.sound_route = ()
        self.sound_step = 0
        self.sound_confirmed_view = None
        self.sound_candidate = None
        self.sound_following = False
        self.sound_follow_motion = None
        self.sound_intent_since = None
        self.last_sound_captured = -math.inf
        self.last_wake_captured = -math.inf

    @staticmethod
    def _game_active(game):
        if not isinstance(game, dict):
            return False
        return (game.get('mode') in MOTION_GAME_MODES
                or game.get('mode') is None and game.get('status') in {'playing', 'calibrating'})

    @staticmethod
    def _game_window(game):
        if not isinstance(game, dict) or 'control_context' not in game:
            return None
        return tuple(game.get(key) for key in ('control_context', 'control_revision', 'round_id'))

    @classmethod
    def _camera_window(cls,sample):
        party=sample.get('party_camera')
        return (cls._game_window(sample.get('game')),
                (party.get('scope'),party.get('revision'))if isinstance(party,dict)else None)

    @classmethod
    def _game_holds(cls,sample):
        party=sample.get('party_camera')
        return cls._game_active(sample.get('game'))or isinstance(party,dict)and party.get('active')is True

    def apply_mode(self, mode, revision):
        """A survey is a finite command window, never a claim of full room coverage."""
        if mode not in CAMERA_MODES:
            raise ValueError('invalid_camera_mode')
        intent = (mode, revision)
        if intent != self.intent:
            self.auto_reconfirmed = None
            self.followed_person = False
            self._discard_patrol()
            self._clear_reframe()
            self._clear_selection_reference()
            self.review_windows.clear()
            self._clear_sound_search()
            self.sound_intent_since = None
            self.tilt_observation = None
            self.tilt_recovery = None
            if self.recovery_steps:
                self.recovery_view_stamp = self.clear_since = None
            if self.intent or self.mode != mode:
                self.target = None
                self.target_identity = None
                self.target_session = None
                self.last_focus = None
                self.target_seen = -100.0
                self.visible_samples = 0
                if (self.mode in PET_MODES) != (mode in PET_MODES):
                    self.last_person = None
                self.pending_decision = None
                self.pending_direction = None
            self.intent = intent
            self.mode = mode
            self.survey_attempts = 0
            self.survey_confirmed_moves = 0
            self.survey_reference = (self.source_session, None) if mode == 'survey' and self.source_session else None
            self.survey_interrupted = False
            self.survey_interruption = None
            self.patrol_view_state = 'idle'
            self.patrol_retry_at = 0.0
            if mode == 'survey':
                self.position = (0, 0)
                self.blocked_positions.clear()
                self.phase = 'checking_view'
            if mode == 'hold':
                self.hold()
            elif mode in TARGET_MODES:
                self.phase = 'target_selecting'

    def restore_survey_progress(self, attempts, confirmed, interruption=None):
        """副镜头重连时保留计数，新采集进程不能接着旧视野移动。"""
        self.survey_attempts, self.survey_confirmed_moves = attempts, confirmed
        self._interrupt_survey(interruption)

    def _interrupt_survey(self, reason=None):
        if self.mode == 'survey' and self.survey_confirmed_moves < len(self.SURVEY_ROUTE):
            self.survey_interrupted = True
            if reason is not None:
                self.survey_interruption = reason

    def _survey_done(self):
        return self.mode == 'survey' and self.sound_search_cue is None and (
            self.survey_interrupted or self.survey_attempts >= self.SURVEY_ATTEMPT_LIMIT
            or self.survey_confirmed_moves >= len(self.SURVEY_ROUTE))

    def _survey_source_current(self, sample):
        session, view = sample.get('session_id'), sample.get('_patrol_view')
        basis = view[0] if view is not None else None
        if self.survey_reference is not None:
            previous_session, previous_basis = self.survey_reference
            if session != previous_session or (previous_basis is not None and basis is not None and basis != previous_basis):
                return False
            if previous_basis is not None:
                return True
        if session:
            self.survey_reference = (session, basis)
        return True

    def _aim(self, direction, sample, now):
        if self.mode == 'survey' and self.sound_search_cue is None:
            self.survey_attempts += 1  # Reserve before dispatch, including unknown/failed outcomes.
        if self.failed_moves >= 3:
            # Reserve a single probe before dispatch, even if its outcome is
            # unknown. Recovery always recomputes from new visual evidence.
            self.retry_at = now + 60
        self.pending_direction = direction
        self.pending_decision = dict(direction=direction, phase=self.phase, intent=self.intent,
            session=sample.get('session_id'), stamp=sample['_local_received'],
            target=self.target, identity=self.target_identity, game_window=self._camera_window(sample))
        if self.phase == 'following':
            self.pending_decision['single_person'] = self.mode == 'auto' and len(self._subjects(sample)) == 1
            subjects = [subject for subject in self._subjects(sample) if subject['id'] == self.target]
            if len(subjects) == 1 and self.sound_following:
                self.pending_decision.update(sound_cue=self.sound_search_cue, sound_box=dict(subjects[0]['box']),
                                             view=sample['_patrol_view'])
            if len(subjects) == 1 and not self.target_identity:
                # Room 子进程复用本模块的视野检查，跟随依赖在实际需要时加载。
                from .camera_follow_appearance import follow_reference_in_view, remember_follow_target

                reference = self.selection_reference
                stamp = sample['_local_received']
                verified = reference if reference and reference[0] == stamp else None
                if verified is None and self.selection_reference_attempt != stamp:
                    appearance = remember_follow_target(sample, subjects[0])
                    if appearance is not None:
                        verified = (stamp, appearance)
                if (verified is None and reference and 0 <= now - reference[0] <= self.FOLLOW_REACQUIRE_SECONDS
                        and follow_reference_in_view(reference[1], sample, subjects[0])):
                    verified = reference
                self.pending_decision['appearance'] = verified
        if self.phase in {'patrolling', 'sound_search'}:
            self.pending_decision['view'] = sample['_patrol_view']
            if self.phase == 'sound_search':
                self.pending_decision['sound_cue'] = self.sound_search_cue
        return direction

    @staticmethod
    def _frame_fresh(sample, now):
        return sample.get('status') == 'live' and 0 <= now-sample.get('_local_received', -100) <= 1.5

    @classmethod
    def _fresh(cls, sample, now):
        return cls._frame_fresh(sample, now) and sample.get('tracking_current') is not False

    def _observe_recovery_view(self, sample, now):
        """从同一采集会话持续核对清晰画面，人物跟踪短暂失效时仍可续接。"""
        stamp = sample.get('_local_received', -100)
        if (sample.get('session_id') != self.source_session or not self._frame_fresh(sample, now)
                or stamp < self.last_move + 2 or sample.get('scene_visible') is not True):
            self.recovery_view_stamp = self.clear_since = None
            return
        previous = self.recovery_view_stamp
        if previous is not None:
            if stamp <= previous:
                return
            if stamp - previous > 1.5:
                self.clear_since = None
        self.recovery_view_stamp = stamp

    def _pets_current(self, sample):
        return (sample.get('animals_current') is True and self.intent is not None
                and sample.get('animals_intent') == self.intent[1]
                and isinstance(sample.get('animals'), list))

    def _subjects(self, sample):
        return sample.get('animals', []) if self.mode in PET_MODES else sample.get('people', [])

    def target_frame_current(self, sample, now, *, stable=True):
        session = sample.get('session_id')
        captured = sample.get('_local_received', -100)
        return (sample.get('status') == 'live' and isinstance(session, str) and bool(session)
                and type(captured) in (int, float) and 0 <= now-captured <= 1.5 and not self._game_holds(sample)
                and (not stable or (captured > self.last_move and sample.get('tracking_current') is True
                                    and sample.get('scene_visible') is True
                                    and sample.get('_follow_stable', True))))

    def select_target(self, sample, subject, now):
        if self.mode not in TARGET_MODES or not self.target_frame_current(sample, now):
            raise ValueError('follow_target_changed')
        self.target = subject['id']
        self.target_identity = self.identity(subject)
        self.target_session = self.source_session = sample['session_id']
        self.target_seen = now
        self.visible_samples = 0
        self.last_sample = sample['_local_received']
        self._clear_selection_reference()
        self._remember_selection(sample, subject)
        self.phase = 'target_reacquiring'

    @staticmethod
    def _qualified_target(target):
        from .stable_tracks import valid_box

        box = target.get('box') if target else None
        return valid_box(box) and box['confidence'] >= .65

    def _clear_selection_reference(self):
        self.selection_sample = self.selection_reference = None
        self.selection_reference_attempt = -math.inf

    def _remember_selection(self, sample, target):
        stamp = sample['_local_received']
        if self.target_identity is not None:
            self.selection_reference = None
            return
        if stamp - self.selection_reference_attempt < self.FOLLOW_REFERENCE_INTERVAL:
            return
        # 更新尝试不能延长上一份有效参考的期限。
        self.selection_reference_attempt = stamp
        from .camera_follow_appearance import remember_follow_target

        appearance = remember_follow_target(sample, target)
        if appearance is not None:
            self.selection_reference = (stamp, appearance)

    def _interrupt_selection(self, now):
        if self.reframe_started is None:
            self.reframe_started = self.target_seen
            reference = self.selection_reference
            if reference and 0 <= now - reference[0] <= self.FOLLOW_REACQUIRE_SECONDS:
                self.reframe_appearance = reference
        self._clear_selection_reference()
        self.reframe_candidate = None
        self.pending_direction = self.pending_decision = None

    def _selected_follow(self, sample, subjects, now):
        """只依据连续观测或重新确认过的原目标跟随。"""
        stamp = sample['_local_received']
        matches = [subject for subject in subjects if subject['id'] == self.target]
        target = matches[0] if len(matches) == 1 else None
        if self.reframe_started is None and (
                not self._qualified_target(target) or self.target_identity
                and self.identity(target) not in (None, self.target_identity)):
            self._interrupt_selection(now)
        if self.reframe_started is not None:
            target = self._reframe_target(sample, subjects)
            if target is None:
                checking = subjects and now - self.reframe_started <= self.FOLLOW_REACQUIRE_SECONDS
                self.phase = 'target_reacquiring' if checking else 'target_lost'
                return None
            self.target = target['id']
            self._clear_reframe()
        elif self.selection_sample is None or stamp - self.selection_sample > 1.5:
            self.selection_sample = stamp
            self.phase = 'target_reacquiring'
            return None
        self.selection_sample = stamp
        self.target_seen = now
        self.target_identity = self.target_identity or self.identity(target)
        self._remember_selection(sample, target)
        return target

    def _clear_reframe(self):
        self.reframe_started = self.reframe_appearance = self.reframe_candidate = None
        self.sound_follow_motion = None

    def _watch_target_after_move(self, pending, now):
        if pending and pending['phase'] == 'following':
            self.single_person_move = pending.get('single_person', False)
            self.followed_person |= self.mode == 'auto'
            self.reframe_started = now
            self.reframe_appearance = pending.get('appearance')
            self.reframe_candidate = None
            self._clear_selection_reference()

    def _reframe_target(self, sample, subjects):
        strict = self.mode in TARGET_MODES or self.sound_following
        if self.target_identity:
            matches = [subject for subject in subjects if self.identity(subject) == self.target_identity]
            match = matches[0] if len(matches) == 1 else None
            if not strict:
                return match
        elif self.reframe_appearance is not None:
            from .camera_follow_appearance import find_follow_target

            match = find_follow_target(self.reframe_appearance[1], sample, subjects,
                                       single_person=self.single_person_move and not strict)
        else:
            match = None
        previous, self.reframe_candidate = self.reframe_candidate, None
        if (match is None or strict and not self._qualified_target(match)
                or sum(row['id'] == match['id'] for row in subjects) != 1):
            return None
        stamp = sample['_local_received']
        candidate = (match['id'], stamp)
        same_target = previous is not None and previous[0] == match['id'] and 0 < stamp - previous[1] <= 1.5
        if self.sound_follow_motion is None:
            self.reframe_candidate = candidate
            return match if same_target else None
        cue, origin, direction = self.sound_follow_motion
        view = sample.get('_patrol_view')
        shift = _view_shift(origin, view)
        if not self._matches_direction(shift, direction) or view[2] <= self.last_move:
            return None
        # 跟随转向后的同一目标必须出现在两次独立配准确认的新视角中。
        if same_target and math.dist(shift, previous[3]) <= .025:
            if view[2] - previous[2] < .35:
                self.reframe_candidate = previous
                return None
            if not isinstance(cue, WakeCameraCue):
                self.sound_confirmed_view = (cue, 'follow', stamp)
            return match
        self.reframe_candidate = (*candidate, view[2], shift)
        return None

    def _follow_direction(self, target):
        box = target.get('box')
        if not isinstance(box, dict):
            return None
        values = tuple(box.get(key) for key in ('x', 'y', 'w', 'h'))
        if any(type(value) not in (int, float) or not 0 <= value <= 1.000001 for value in values):
            return None
        left, top, width, height = values
        right, bottom = 1 - left - width, 1 - top - height
        if not width or not height or min(right, bottom) < -.000001:
            return None
        horizontal_error = left + width / 2 - .5
        horizontal = ('right' if horizontal_error > 0 else 'left') if abs(horizontal_error) > .20 else None
        vertical_error = top + height / 2 - .5
        vertical_margin = min(top, bottom)
        # 目标贴近上下边缘且明显偏向一侧才俯仰。两轴都偏离时，先保住更近的边缘。
        if (vertical_margin >= .12 or abs(vertical_error) <= .12
                or horizontal is not None and vertical_margin + .04 >= min(left, right)):
            return horizontal
        tilt = 'down' if vertical_error > 0 else 'up'
        return horizontal if self._next_position(tilt) in self.blocked_positions else tilt

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

    def decision_current(self, sample, now, direction, *, focus=None, occupied_elsewhere=False,
                         review_pending=False, fall_pending=False, sound_cue=None):
        """Recheck the pending purpose without consuming evidence or survey budget."""
        pending = self.pending_decision
        if (not pending or direction != self.pending_direction or self.mode == 'hold'
                or self._game_holds(sample)
                or pending.get('game_window') != self._camera_window(sample)
                or pending['intent'] != self.intent or not self._fresh(sample, now)
                or sample.get('session_id') != pending['session']
                or sample['_local_received'] < pending['stamp']
                or sample['_local_received'] <= self.last_move):
            return False
        if self._review_holds(sample, now, review_pending, fall_pending):
            return False
        if self._wake_waits_for_voice(sample, now, sound_cue):
            return False
        if pending.get('sound_cue') is not None and (
                self.mode not in {'auto', 'survey'} or sound_cue is not pending['sound_cue']
                or sound_cue is not self.sound_search_cue or review_pending or fall_pending):
            return False
        if isinstance(sound_cue, WakeCameraCue):
            if pending.get('sound_cue') is sound_cue:
                if not self._wake_frame_current(sample, now, sound_cue):
                    return False
            elif (not (review_pending or fall_pending) and pending['phase'] in {'patrolling', 'following'}
                  and self._sound_start_available(sample, now, sound_cue)):
                return False
        if self.mode in PET_MODES and not self._pets_current(sample):
            return False
        explicit = self.mode in TARGET_MODES
        if explicit and (pending['phase'] != 'following' or self.target_session != sample.get('session_id')
                         or sample.get('tracking_current') is not True or self.reframe_started is not None):
            return False
        if pending['phase'] in {'uncovering', 'recovering_view'}:
            return sample.get('scene_visible') is False
        if sample.get('scene_visible') is not True:
            return False
        people = self._subjects(sample)
        if pending['phase'] in {'patrolling', 'sound_search'}:
            if pending['phase'] == 'patrolling' and self.mode == 'auto' and self.recovery_steps:
                return False
            shift = _view_shift(pending['view'], sample.get('_patrol_view'))
            if self.mode == 'survey' and pending['phase'] == 'patrolling':
                # 已就绪的异响可撤销尚未发出的普通巡视步进。
                if (not people and not (review_pending or fall_pending)
                        and self._sound_start_available(sample, now, sound_cue)):
                    return False
            else:
                if self.mode == 'survey' and not self.target_frame_current(sample, now):
                    return False
                if people or pending['phase'] == 'patrolling' and occupied_elsewhere:
                    return False
            return shift is not None and math.hypot(*shift) <= .012
        if pending['phase'] != 'following':
            return False
        sound_follow = 'sound_box' in pending
        if sound_follow and (not self.sound_following or len(people) != 1
                             or self._sound_scope(sample) != self.sound_view
                             or not self.target_frame_current(sample, now)):
            return False
        focus = self._current_focus(sample, now, focus)
        if (self.mode in {'auto', 'survey'} and focus
                and focus.get('track') != pending['target']):
            return False
        tilt = direction in self.TILT_DIRECTIONS
        if tilt and self.tilt_observation != (
                (pending['session'], pending['target'], direction, pending['game_window']), pending['stamp']):
            return False
        matches = [p for p in people if p['id'] == pending['target']]
        if not matches and pending['identity'] and not (tilt or explicit or sound_follow):
            matches = [p for p in people if self.identity(p) == pending['identity']]
        if len(matches) != 1:
            return False
        target = matches[0]
        if (explicit or sound_follow) and not self._qualified_target(target):
            return False
        if sound_follow:
            shift = _view_shift(pending['view'], sample.get('_patrol_view'))
            if not iou(pending['sound_box'], target['box']) or shift is None or math.hypot(*shift) > .012:
                return False
        if pending['identity'] and self.identity(target) and self.identity(target) != pending['identity']:
            return False
        return self._follow_direction(target) == direction

    def _next_position(self, direction):
        x,y = self.position
        return (x+(direction=='right')-(direction=='left'), y+(direction=='up')-(direction=='down'))

    def hold(self):
        # Manual positioning changes the relative origin. Do not reuse old angles.
        self._discard_patrol()
        self._clear_reframe()
        self._clear_selection_reference()
        self._clear_sound_search()
        self.position = (0,0)
        self.blocked_positions.clear()
        self.scan_steps = 0
        self.pending_direction = None
        self.pending_decision = None
        self.tilt_observation = None
        self.tilt_recovery = None
        self.visible_samples = 0
        self.recovery_steps = 0
        self.recovery_retry_at = 0.0
        self.recovery_view_stamp = None
        self.obscured_since = None
        self.obscured_samples = 0
        self.phase = 'held'
        self.review_windows.clear()

    def _discard_patrol(self, *, uncertain_dispatch=False):
        if self.patrol_confirmation is None and not uncertain_dispatch:
            return
        self._interrupt_survey()
        if self.patrol_confirmation and self.patrol_confirmation.get('sound_search'):
            self._clear_sound_search()
        self.patrol_confirmation = None
        self.patrol_view_state = 'unconfirmed'
        # 转过多少未知，相对角度及其遮挡记录已失去基准。
        self.position = (0, 0)
        self.scan_steps = 0
        self.blocked_positions.clear()

    def _recover_view(self, sample, now):
        if now < self.recovery_retry_at:
            self.phase = 'view_recovery_waiting'
            return None
        step = self.recovery_steps
        if step == 0:
            self.recovery_pan_reversed = self.last_direction != 'right'
        self.phase = 'recovering_view'
        if self.tilt_recovery is not None:
            # 跟随之后先撤回同一游戏状态下的俯仰。
            direction, camera_window = self.tilt_recovery
            self.tilt_recovery = None
            if camera_window == self._camera_window(sample):
                return self._aim(direction, sample, now)
        if step >= len(self.UNCOVER_ROUTE):
            attempt, offset = divmod(step - len(self.UNCOVER_ROUTE), len(self.VIEW_RECOVERY_PROBES[0]))
            direction = self.VIEW_RECOVERY_PROBES[attempt % len(self.VIEW_RECOVERY_PROBES)][offset]
        elif not self.clear:
            direction = self.UNCOVER_ROUTE[step]
            if direction == 'up':
                self.phase = 'uncovering'
        else:
            direction = self.VIEW_RECOVERY_ROUTE[step]
            if self.recovery_pan_reversed and direction in ('left', 'right'):
                direction = 'right' if direction == 'left' else 'left'
        return self._aim(direction, sample, now)

    def take_sound_view(self):
        proof, self.sound_confirmed_view = self.sound_confirmed_view, None
        return proof

    def _matches_direction(self, shift, direction):
        axis, sign = self.PATROL_AXES[direction]
        return (shift is not None and sign * shift[axis] >= .025
                and abs(shift[axis]) >= 1.5 * abs(shift[1-axis]))

    def _defer_auto_patrol(self, pending, now):
        # 相对角度无法确认，暂缓下一次自动巡视。互动和清晰视野恢复仍从当前画面判断。
        self._discard_patrol()
        self.scan_direction = 'left' if pending['direction'] == 'right' else 'right'
        self.patrol_retry_at = now + self.AUTO_PATROL_RETRY_SECONDS

    def _confirm_patrol(self, sample, now, sound_cue=None, *, observation_requested=False):
        pending = self.patrol_confirmation
        if pending is None:
            return False
        session = sample.get('session_id')
        if ((session is not None and session != pending['session'])
                or self._camera_window(sample) != pending['game_window']):
            self._discard_patrol()
            return False
        ordinary = self.mode == 'auto' and not pending['sound_search']
        if now >= pending['deadline']:
            if ordinary:
                self._defer_auto_patrol(pending, now)
                return False
            # 退避从确认超时起算，画面稳定和巡视间隔仍从实际发出转向起算。
            last_move = self.last_move
            self.failed(now)
            self.last_move = last_move
            return True
        self.phase = 'confirming_view'
        if (session != pending['session'] or not self._fresh(sample, now)
                or sample['_local_received'] <= pending['stamp']):
            return True
        stamp = sample['_local_received']
        pending['stamp'] = stamp
        if stamp < self.last_move + 2:
            return True
        visible = sample.get('scene_visible')
        if ordinary and (visible is False or visible is True and observation_requested):
            self._defer_auto_patrol(pending, now)
            return False
        view = sample.get('_patrol_view')
        if ((ordinary or self.mode == 'survey' or pending['sound_search'])
                and view is not None and view[0] != pending['view'][0]):
            if ordinary:
                self._defer_auto_patrol(pending, now)
            else:
                self._discard_patrol()
            return False
        shift = _view_shift(pending['view'], view) if visible is True else None
        moved = self._matches_direction(shift, pending['direction'])
        steady = (ordinary and shift is not None and view[2] >= self.last_move + 2
                  and math.hypot(*shift) <= .012)
        if not moved and not steady:
            pending['previous'] = None
            return True
        previous = pending['previous']
        if previous is not None and previous[3] == moved:
            previous_stamp, previous_view_at, previous_shift, _ = previous
            # 配准最多每 0.3 秒计算一次。两次采样必须跨过复用窗口。
            if view[2] - previous_view_at < .35:
                return True
            if stamp - previous_stamp <= 3.5 and math.dist(shift, previous_shift) <= .025:
                if steady:
                    # 当前画面清晰且连续接近起始视角，可以恢复观察。巡视步进仍未确认。
                    self._defer_auto_patrol(pending, now)
                    self.visible_samples = 1
                    return False
                self.patrol_confirmation = None
                self.patrol_view_state = 'confirmed'
                self.failed_moves = 0
                self.retry_at = 0.0
                self.position = self._next_position(pending['direction'])
                if pending.get('sound_search'):
                    self.sound_step += 1
                    if (sound_cue is not None and not isinstance(sound_cue, WakeCameraCue)
                            and sound_cue is self.sound_search_cue
                            and sample.get('tracking_current') is True):
                        self.sound_confirmed_view = (sound_cue, self.sound_step, stamp)
                elif self.mode == 'survey':
                    self.survey_confirmed_moves += 1
                else:
                    self.scan_steps += 1
                    if self.scan_steps >= 12:
                        self.scan_direction = 'left' if self.scan_direction == 'right' else 'right'
                        self.scan_steps = 0
                self.visible_samples = 1
                return False
        pending['previous'] = (stamp, view[2], shift, moved)
        return True

    def _review_window(self, key, scope, now, duration, interval, reserve):
        window = self.review_windows.get(key)
        if window is not None:
            previous_scope, until, retry_at = window
            if previous_scope == scope and now < retry_at:
                return now < until
        if reserve:
            self.review_windows[key] = (scope, now + duration, now + interval)
        return True

    def _review_holds(self, sample, now, review_pending, fall_pending, *, reserve=False):
        if not self._fresh(sample, now) or sample.get('scene_visible') is not True:
            return False
        scope = (self.intent, sample.get('session_id'))
        if reserve:
            self.review_windows = {key: window for key, window in self.review_windows.items()
                                   if window[0] == scope and now < window[2]}
        falls = fall_pending if isinstance(fall_pending, frozenset) else ((None,) if fall_pending is True else ())
        holding = False
        for key in falls:
            holding = self._review_window(('fall', key), scope, now, self.FALL_HOLD_SECONDS,
                                          self.FALL_INTERVAL_SECONDS, reserve) or holding
        if holding:
            return True
        return (review_pending is True and (self.mode in {'auto', 'survey'} or self.mode in TARGET_MODES)
                and self._review_window('review', scope, now, self.REVIEW_HOLD_SECONDS,
                                        self.REVIEW_INTERVAL_SECONDS, reserve))

    def _patrol(self, sample, now):
        session = sample.get('session_id')
        if not isinstance(session, str) or not session or sample.get('_patrol_view') is None:
            self.phase = 'patrol_waiting_view'
            return None
        if self.mode == 'survey':
            direction = self.SURVEY_ROUTE[self.survey_confirmed_moves]
        else:
            if self._next_position(self.scan_direction) in self.blocked_positions:
                self.scan_direction = 'left' if self.scan_direction == 'right' else 'right'
            if self._next_position(self.scan_direction) in self.blocked_positions:
                self.phase = 'observing'
                return None
            direction = self.scan_direction
        self.phase = 'patrolling'
        return self._aim(direction, sample, now)

    def _clear_sound_search(self):
        self.sound_search_cue = self.sound_candidate = None
        self.sound_following = False
        self.sound_follow_motion = None

    def _end_sound_search(self, now):
        if self.sound_search_cue is not None:
            self.last_person = now
            self._clear_sound_search()

    def _sound_scope(self, sample):
        view = sample.get('_patrol_view')
        return (sample.get('session_id'), view[0], self._camera_window(sample)) if view is not None else None

    def _wake_source_current(self, sample, cue):
        return (self.mode == 'auto' and sample.get('session_id') == cue.session
                and sample.get('_wake_source') == cue.source)

    def _wake_frame_current(self, sample, now, cue):
        return self._wake_source_current(sample, cue) and self.target_frame_current(sample, now)

    def _wake_waits_for_voice(self, sample, now, sound_cue):
        if not isinstance(sound_cue, WakeCameraPause):
            return False
        cue = sound_cue.cue
        if self.sound_search_cue is cue:
            scope = self._sound_scope(sample)
            return self._wake_source_current(sample, cue) and (scope is None or scope == self.sound_view)
        return self._sound_start_available(sample, now, cue)

    def _sound_start_available(self, sample, now, cue):
        if cue is None or isinstance(cue, WakeCameraPause):
            return False
        wake = isinstance(cue, WakeCameraCue)
        last_captured = self.last_wake_captured if wake else self.last_sound_captured
        return (self.sound_search_cue is None and self._sound_scope(sample) is not None
                and self.sound_intent_since is not None
                and cue.captured > last_captured and cue.captured >= self.sound_intent_since
                and (not wake or self._wake_frame_current(sample, now, cue))
                and (self.mode != 'survey' or (not self._survey_done() and self.target_frame_current(sample, now))))

    def _begin_sound_search(self, sample, cue):
        wake = isinstance(cue, WakeCameraCue)
        if wake:
            self.last_wake_captured = cue.captured
            self._clear_reframe()
        else:
            self.last_sound_captured = cue.captured
        first = self.scan_direction
        other = 'left' if first == 'right' else 'right'
        if self._next_position(first) in self.blocked_positions:
            first, other = other, first
        if not wake and self._next_position(first) in self.blocked_positions:
            return
        self.sound_search_cue = cue
        self.sound_view = self._sound_scope(sample)
        self.sound_route = (first, other, other)
        self.sound_step = 0
        self._interrupt_survey('sound')

    def _sound_search(self, sample, now, *, may_search):
        if self.sound_search_cue is None:
            return None
        if not may_search or self.sound_step >= len(self.sound_route):
            self._end_sound_search(now)
            return None
        direction = self.sound_route[self.sound_step]
        if self._next_position(direction) in self.blocked_positions:
            self._end_sound_search(now)
            return None
        if self._sound_scope(sample) is None:
            self.phase = 'patrol_waiting_view'
            return None
        self.phase = 'sound_search'
        return self._aim(direction, sample, now)

    def _sound_follow_target(self, sample, people, now):
        """同一新视角的连续人体观测才能接手，异响回执不指认声源。"""
        previous, self.sound_candidate = self.sound_candidate, None
        target = people[0] if len(people) == 1 else None
        if (self._sound_scope(sample) != self.sound_view or not self.target_frame_current(sample, now)
                or not self._qualified_target(target)):
            if self.sound_following:
                self._interrupt_selection(now)
            self.phase = 'target_ambiguous'
            return None
        if self.sound_following:
            return self._selected_follow(sample, people, now)
        stamp = sample['_local_received']
        identity = self.identity(target)
        self.sound_candidate = (target['id'], stamp, identity, dict(target['box']), sample['_patrol_view'])
        self.phase = 'target_ambiguous'
        if previous is None:
            return None
        track, observed, previous_identity, box, view = previous
        shift = _view_shift(view, sample['_patrol_view'])
        if (track != target['id'] or not 0 < stamp - observed <= 1.5
                or previous_identity and identity and previous_identity != identity
                or not iou(box, target['box']) or shift is None or math.hypot(*shift) > .012):
            return None
        self.sound_following = True
        self.sound_candidate = None
        self.target = target['id']
        self.target_identity = identity or previous_identity
        self.target_session = sample['session_id']
        self.target_seen = now
        self._clear_reframe()
        self._clear_selection_reference()
        self.selection_sample = stamp
        self._remember_selection(sample, target)
        return target

    @staticmethod
    def identity(person):
        identity = person.get('identity') or {}
        return identity.get('person_id') if identity.get('state') in {'matched', 'tracked'} else None

    def ordinary_patrol_pending(self):
        return (self.mode == 'auto' and self.pending_decision is not None
                and self.pending_decision['phase'] == 'patrolling')

    def defer_patrol(self):
        self.pending_direction = self.pending_decision = None
        self.phase = 'patrol_deferred'

    def choose(self, sample, now, *, focus=None, occupied_elsewhere=False, review_pending=False, fall_pending=False,
               sound_cue=None, ordinary_patrol_allowed=True):
        # 镜头稳定等待期间也要撤销单人前提，避免两人交错后误跟。
        if self.reframe_started is not None and len(sample.get('people') or []) > 1:
            self.single_person_move = False
        self.sound_confirmed_view = None
        if self.mode == 'hold':
            self.phase = 'held'
            self.tilt_observation = None
            return None
        if self._game_holds(sample):
            # Keep the selected camera stable for the whole admitted game,
            # including waiting, pause and results. Perception still consumes
            # frames; neither clipped players nor an obscured view may pan it.
            self.game_hold = True
            self._end_sound_search(now)
            self.sound_intent_since = now
            self._interrupt_survey()
            self._discard_patrol()
            self.pending_direction = self.pending_decision = None
            self.tilt_observation = None
            self.tilt_recovery = None
            self.target = self.target_identity = self.target_session = None
            self._clear_reframe()
            self._clear_selection_reference()
            self.last_focus = None
            self.visible_samples = self.obscured_samples = 0
            self.clear_since = self.obscured_since = None
            self.recovery_view_stamp = None
            fresh = self._fresh(sample, now)
            if fresh:
                self.source_session = sample.get('session_id')
                self.last_sample = (sample['_local_received'] if self.last_sample is None
                                    else max(self.last_sample, sample['_local_received']))
                self.last_person = now
            self.phase = 'game_held' if fresh else 'waiting_video'
            return None
        explicit = self.mode in TARGET_MODES
        if explicit and (self.target_session is None or self.target_session != sample.get('session_id')):
            self.phase = 'target_selecting' if self.target_session is None else 'target_lost'
            self._clear_reframe()
            self._clear_selection_reference()
            return None
        if (self.selection_reference is not None
                and now - self.selection_reference[0] > self.FOLLOW_REACQUIRE_SECONDS):
            self.selection_reference = None
        if ((self.reframe_started is not None and now - self.reframe_started > self.FOLLOW_REACQUIRE_SECONDS)
                or (self.reframe_appearance is not None
                    and now - self.reframe_appearance[0] > self.FOLLOW_REACQUIRE_SECONDS)):
            self.reframe_appearance = self.reframe_candidate = None
        if self.sound_intent_since is None:
            self.sound_intent_since = now
        if self.game_hold:
            self.game_hold = False
            self.sound_intent_since = now
            self.clear = False
            self.visible_samples = self.obscured_samples = 0
            self.clear_since = self.obscured_since = None
            self.recovery_view_stamp = None
            self.phase = 'checking_view'
        wake_waiting = self._wake_waits_for_voice(sample, now, sound_cue)
        confirming_cue = sound_cue.cue if wake_waiting else sound_cue
        patrol = self.patrol_confirmation
        observation_requested = (
            patrol is not None and self.mode == 'auto' and not patrol['sound_search']
            and (occupied_elsewhere or bool(sample.get('people')) or wake_waiting
                 or self._review_holds(sample, now, review_pending, fall_pending)
                 or self._sound_start_available(sample, now, sound_cue))
        )
        if self._confirm_patrol(sample, now, confirming_cue, observation_requested=observation_requested):
            return None
        if self._survey_done():
            self.phase = 'survey_finished'
            return None
        if self.failed_moves >= 3 and now < self.retry_at:
            self.visible_samples = 0
            self.obscured_samples = 0
            self.obscured_since = None
            self.tilt_observation = None
            self.phase = 'motor_unavailable'
            return None
        checking_recovery_view = self.mode == 'auto' and self.recovery_steps > 0
        if checking_recovery_view:
            self._observe_recovery_view(sample, now)
        if (not self._fresh(sample, now) or sample['_local_received'] <= self.last_move
                or (explicit or self.sound_following) and sample.get('tracking_current') is not True):
            self.sound_candidate = None
            if explicit or self.sound_following:
                self._interrupt_selection(now)
            self.visible_samples = self.obscured_samples = 0
            self.obscured_since = None
            if not checking_recovery_view:
                self.clear_since = None
            self.tilt_observation = None
            self.phase = 'waiting_video'
            return None
        if now-self.last_move < 2:
            self.phase = 'settling'
            return None
        if self.mode == 'survey' and self.sound_search_cue is None and not self._survey_source_current(sample):
            self._interrupt_survey()
            self.phase = 'survey_finished'
            return None
        if self.mode in PET_MODES:
            if not self._pets_current(sample):
                if explicit:
                    self._interrupt_selection(now)
                self.phase = 'pet_unavailable'
                self.tilt_observation = None
                return None
        session = sample.get('session_id')
        if session != self.source_session:
            self.auto_reconfirmed = None
            self.followed_person = False
            self._end_sound_search(now)
            self.source_session = session
            self.target = self.target_identity = self.last_focus = None
            self._clear_reframe()
            self.last_sample = None
            self.clear = False
            self.visible_samples = self.obscured_samples = 0
            self.clear_since = self.obscured_since = None
            self.recovery_steps = self.scan_steps = 0
            self.recovery_retry_at = 0.0
            self.recovery_view_stamp = None
            checking_recovery_view = False
            self.recovery_pan_reversed = False
            self.patrol_retry_at = 0.0
            self.last_direction = None
            self.position = (0, 0)
            self.blocked_positions.clear()
            self.pending_direction = self.pending_decision = None
            self.tilt_observation = None
            self.tilt_recovery = None
        stamp = sample.get('_local_received')
        if self.last_sample is not None and stamp <= self.last_sample:
            return None
        if self.last_sample is not None and stamp - self.last_sample > 1.5:
            self.obscured_samples = 0
            self.obscured_since = None
            if self.recovery_steps and not checking_recovery_view:
                self.clear_since = None
            if explicit or self.sound_following:
                self._interrupt_selection(now)
        self.last_sample = stamp
        previous_tilt, self.tilt_observation = self.tilt_observation, None
        visible = sample.get('scene_visible') is True
        self.visible_samples = self.visible_samples+1 if visible else 0
        if sample.get('scene_visible') is None:
            self.obscured_samples = 0
            self.clear_since = self.obscured_since = None
            self.phase = 'checking_view'
            return None
        if not visible:
            self._end_sound_search(now)
            self.clear_since = None
            self.obscured_samples += 1
            if self.obscured_since is None:self.obscured_since = now
            self.phase = 'view_obscured'
            self.reframe_candidate = None
            if explicit:
                self._interrupt_selection(now)
                return None
            if self.obscured_samples < 3 or now-self.obscured_since < 1:
                return None
            if self._survey_done() or self.mode == 'survey' and self.survey_confirmed_moves:
                self._interrupt_survey()
                self.phase = 'survey_finished'
                return None
            self.blocked_positions.add(self.position)
            if len(self.blocked_positions)>64:self.blocked_positions.pop()
            return self._recover_view(sample, now)
        self.obscured_samples = 0
        self.obscured_since = None
        people = self._subjects(sample)
        target = self._selected_follow(sample, people, now) if explicit else None
        if self.visible_samples < 2:
            if not explicit:
                self.phase = 'checking_view'
            return None
        self.tilt_recovery = None
        self.clear = True
        self.blocked_positions.discard(self.position)
        clear_at = stamp if checking_recovery_view else now
        if self.clear_since is None:
            self.clear_since = clear_at
        if clear_at - self.clear_since >= 20:
            self.recovery_steps = 0
            self.recovery_retry_at = 0.0
            self.recovery_view_stamp = None
        if self._review_holds(sample, now, review_pending, fall_pending, reserve=True):
            self.pending_direction = self.pending_decision = None
            self.reframe_candidate = None
            self.sound_candidate = None
            self.phase = 'review_held'
            return None
        if wake_waiting:
            self.pending_direction = self.pending_decision = None
            self.sound_candidate = self.reframe_candidate = self.tilt_observation = None
            self.phase = 'checking_view'
            return None
        if self.mode in {'auto', 'survey'}:
            focus = self._current_focus(sample, now, focus)
            wake = isinstance(sound_cue, WakeCameraCue)
            safety_pending = review_pending or fall_pending
            may_search = not people and not safety_pending
            if self.sound_search_cue is not None:
                scope = self._sound_scope(sample)
                if (sound_cue is not self.sound_search_cue or scope is not None and scope != self.sound_view
                        or wake and safety_pending
                        or focus and (not self.sound_following or focus['track'] != self.target)):
                    self._end_sound_search(now)
            if (not safety_pending and (wake or not people and self.reframe_started is None)
                    and self._sound_start_available(sample, now, sound_cue)):
                self._begin_sound_search(sample, sound_cue)
            active = self.sound_search_cue
            if active is not None and (self.sound_step or wake) and (people or self.sound_following):
                target = self._sound_follow_target(sample, people, now)
                if target is None:
                    return None
            elif self.reframe_started is None:
                self.sound_candidate = None
                direction = self._sound_search(sample, now, may_search=may_search)
                if direction or self.sound_search_cue is not None:
                    return direction
        if self.mode == 'survey' and not self.sound_following:
            if self._survey_done():
                self.phase = 'survey_finished'
                return None
            if now - self.last_move < 12 or (not self.survey_confirmed_moves and now - self.clear_since < 15):
                self.phase = 'observing'
                return None
            return self._patrol(sample, now)
        if not explicit and not self.sound_following:
            ids = {p['id'] for p in people}
            self.target_session = session
            if self.mode == 'auto' and focus and focus.get('track') in ids:
                key = (session, focus['at'], focus['track'])
                if key != self.last_focus:
                    self.target = focus['track']; self.last_focus = key
                    self.target_identity = None
                    self._clear_reframe()
            if self.target is None and len(people) == 1:
                self.target = people[0]['id']
            target = next((p for p in people if p['id'] == self.target), None)
            if (target and self.target_identity and self.identity(target)
                    and self.identity(target) != self.target_identity):
                target = None
            if self.reframe_started is not None:
                target = self._reframe_target(sample, people)
                if target is not None:
                    self.target = target['id']
                    self.auto_reconfirmed = target['id'] if self.mode == 'auto' and self.single_person_move else None
                    self._clear_reframe()
                elif people or now - self.reframe_started <= self.FOLLOW_REACQUIRE_SECONDS:
                    if people:
                        self.last_person = now
                    self.phase = ('target_reacquiring' if now - self.reframe_started <= self.FOLLOW_REACQUIRE_SECONDS
                                  else 'target_lost')
                    return None
                else:
                    self._clear_reframe()
            elif target is None and self.target_identity:
                matches = [p for p in people if self.identity(p) == self.target_identity]
                if len(matches) == 1:
                    target = matches[0]
                    self.target = target['id']
            elif target is None and len(people) == 1:
                # 静止视野只有一个目标时，沿用自动跟随的原选择方式。
                target = people[0]
                self.target = target['id']
        if people:
            self.last_person = now
            if target is None:
                if not explicit:
                    self.phase = 'target_ambiguous'
                return None
            if not explicit:
                self.target_seen = now
                self.target_identity = self.target_identity or self.identity(target)
            self.phase = 'following'
            # Allow fresh post-pan face evidence to accumulate before another
            # impulse discards it. A matched identity may release this wait;
            # an unrecognizable target never stalls following indefinitely.
            if self.auto_reconfirmed != target['id'] and not self.identity(target) and now-self.last_move < 4:
                return None
            direction = self._follow_direction(target)
            if direction in self.TILT_DIRECTIONS:
                if not isinstance(session, str) or not session or sum(p['id'] == target['id'] for p in people) != 1:
                    return None
                target_view = (session, target['id'], direction, self._camera_window(sample))
                self.tilt_observation = (target_view, stamp)
                # 两次连续观测必须属于同一来源、同一跟踪框和同一转向。
                if (previous_tilt is None or previous_tilt[0] != target_view
                        or not 0 < stamp - previous_tilt[1] <= 1.5):
                    return None
            return self._aim(direction, sample, now) if direction else None
        if explicit:
            return None
        if self.last_person is None:
            self.last_person = now
        if now-self.target_seen > 8:
            self.target = None
            self.target_identity = None
        if self.mode == 'auto' and self.followed_person:
            self.phase = 'target_lost'
            return None
        # 刚恢复的视野先保持到连续观测重置恢复计数，有效唤醒和跟随已在上面处理。
        if (occupied_elsewhere or now < self.patrol_retry_at or (self.mode == 'auto' and self.recovery_steps)
                or now-self.last_person < 15 or now-self.last_move < 12):
            self.phase = 'observing'
            return None
        if self.mode == 'auto' and not ordinary_patrol_allowed:
            self.defer_patrol()
            return None
        return self._patrol(sample, now)

    def sent(self, now, direction=None, *, automatic=True):
        self.last_move = now
        if self.recovery_steps:
            self.recovery_view_stamp = self.clear_since = None
        self.auto_reconfirmed = None
        if not automatic:
            self.last_wake_captured = max(self.last_wake_captured, now)
            self._end_sound_search(now)
        direction=direction or self.pending_direction
        pending = self.pending_decision
        if automatic and direction == self.pending_direction:
            self._watch_target_after_move(pending, now)
            if pending and 'sound_box' in pending:
                self.sound_follow_motion = (pending['sound_cue'], pending['view'], direction)
        patrol = automatic and direction == self.pending_direction and pending and pending['phase'] in {'patrolling', 'sound_search'}
        if patrol:
            self.patrol_confirmation = dict(direction=direction, session=pending['session'],
                game_window=pending['game_window'], view=pending['view'],
                stamp=now, deadline=now + self.PATROL_CONFIRM_SECONDS, previous=None,
                sound_search=pending['phase'] == 'sound_search')
            self.patrol_view_state = 'checking'
            if self.mode == 'auto' and pending['phase'] == 'patrolling':
                self.failed_moves = 0
                self.retry_at = 0.0
        else:
            if self.survey_confirmed_moves:
                self._interrupt_survey()
            self._discard_patrol()
            self.failed_moves = 0
            self.retry_at = 0.0
        self.tilt_recovery = None
        if (automatic and direction in self.TILT_DIRECTIONS and direction == self.pending_direction
                and self.pending_decision and self.pending_decision['phase'] == 'following'):
            reverse = 'up' if direction == 'down' else 'down'
            self.tilt_recovery = (reverse, self.pending_decision['game_window'])
        if direction in {'up','down','left','right'}:
            if not patrol:
                self.position=self._next_position(direction)
            self.last_direction=direction
        self.pending_direction=None
        self.pending_decision=None
        self.tilt_observation = None
        if (automatic and pending and direction == pending['direction']
                and pending['phase'] in {'uncovering', 'recovering_view'}):
            self.recovery_steps += 1
            probe_steps = self.recovery_steps - len(self.UNCOVER_ROUTE)
            if probe_steps >= 0:
                attempt, remaining = divmod(probe_steps, len(self.VIEW_RECOVERY_PROBES[0]))
                if remaining == 0:
                    delay = self.VIEW_RECOVERY_DELAYS[min(attempt, len(self.VIEW_RECOVERY_DELAYS) - 1)]
                    self.recovery_retry_at = now + delay
            if pending['phase'] == 'uncovering':
                self.uncover_steps += 1
        # Every impulse requires a new stable multi-frame view, never the frame
        # that caused that impulse. No queued or timed blind motor sequence.
        self.visible_samples=0
        self.obscured_samples=0
        self.obscured_since=None

    def failed(self, now):
        pending = self.pending_decision
        self._watch_target_after_move(pending, now)
        if self.survey_confirmed_moves:
            self._interrupt_survey()
        self._end_sound_search(now)
        self._discard_patrol(uncertain_dispatch=pending is not None and pending['phase'] in {'patrolling', 'sound_search'})
        self.last_move = now
        if self.recovery_steps:
            self.recovery_view_stamp = self.clear_since = None
        self.failed_moves += 1
        if self.failed_moves >= 3:
            self.retry_at = now + 60
        self.pending_direction = None
        self.pending_decision = None
        self.tilt_observation = None
        self.visible_samples = 0
        self.obscured_samples = 0
        self.obscured_since = None
        self.phase = 'motor_unavailable'

    def public(self):
        reason = {'waiting_video':'等待画面', 'settling':'镜头稳定中',
                  'checking_view':'确认视野', 'uncovering':'镜头唤醒中',
                  'recovering_view':'调整视野', 'view_recovery_waiting':'视野受阻，等待重试',
                  'view_obscured':'视野受阻', 'motor_unavailable':'云台待恢复',
                  'following':'跟随人物', 'target_ambiguous':'等待确认目标',
                  'target_reacquiring':'正在确认原目标', 'target_lost':'未找到原目标', 'target_selecting':'等待选择目标',
                  'patrolling':'环视房间', 'observing':'静候互动', 'held':'停留当前视角',
                  'patrol_deferred':'保留当前视角',
                  'sound_search':'异响后环视',
                  'confirming_view':'确认新视角', 'patrol_waiting_view':'等待清晰视角',
                  'survey_finished':'本轮巡视已结束', 'pet_unavailable':'宠物识别暂不可用',
                  'game_held':'游戏中，镜头保持稳定', 'review_held':'停留观察'}[self.phase]
        if self.phase == 'following' and self.mode in PET_MODES:reason = '跟随宠物'
        if self.mode in TARGET_MODES:
            reason = {'following': '跟随所选宠物' if self.mode == 'pet_target' else '跟随所选人物',
                      'target_lost': '未找到所选目标', 'target_reacquiring': '正在确认所选目标'}.get(self.phase, reason)
        if isinstance(self.sound_search_cue, WakeCameraCue):
            reason = {'sound_search': '已唤醒，镜头正在找人', 'confirming_view': '已唤醒，镜头正在找人',
                      'checking_view': '已唤醒，正在确认画面', 'patrol_waiting_view': '已唤醒，正在确认画面',
                      'following': '已唤醒，镜头跟随中', 'target_reacquiring': '已唤醒，正在重新对准',
                      'target_ambiguous': '已唤醒，正在确认目标'}.get(self.phase, reason)
            if self.phase == 'following' and self.pending_direction is not None:
                reason = '已唤醒，镜头正在转向'
        elif self.sound_search_cue is not None and self.phase in {'confirming_view', 'checking_view', 'patrol_waiting_view'}:
            reason = '异响后环视'
        if self.mode == 'survey' and self.sound_following and self.phase == 'following':
            reason = '异响后跟随人物'
        if self.phase == 'patrolling' and self.mode == 'survey':
            reason = {'down': '巡视低处', 'up': '抬回视野'}.get(self.pending_direction, reason)
        elif self.phase == 'observing' and self.mode == 'survey':
            reason = '巡视中，观察当前视角'
        report = dict(owner='friday', phase=self.phase, reason=reason, native_tracking=False,
                    intent_revision=self.intent[1] if self.intent else None,
                    native_patrol=False, view_usable=self.clear and self.phase in {
                        'following','target_ambiguous','patrolling','sound_search','observing','game_held','pet_unavailable',
                        'review_held','patrol_waiting_view','patrol_deferred','target_reacquiring','target_lost'},
                    uncover_steps=self.uncover_steps, recovery_steps=self.recovery_steps,
                    survey_attempts=self.survey_attempts, survey_command_limit=self.SURVEY_ATTEMPT_LIMIT,
                    survey_confirmed_moves=self.survey_confirmed_moves, survey_total_steps=len(self.SURVEY_ROUTE),
                    survey_result=None, survey_interruption=self.survey_interruption,
                    patrol_view_state=self.patrol_view_state,
                    avoided_angles=len(self.blocked_positions), target_locked=self.phase == 'following',
                    physical_motion_verified=False)
        if self.phase == 'survey_finished':
            return finish_camera_survey(report)
        if self.mode == 'survey' and self.game_hold:
            finished = finish_camera_survey(report)
            report.update(survey_result=finished['survey_result'], reason=finished['reason'])
        return report
