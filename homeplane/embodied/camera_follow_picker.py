"""在当前采集会话内，用同一帧的画面和目标框选定跟随对象。"""

import base64
from dataclasses import dataclass
import secrets
import time
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from .camera_intent import TARGET_MODES


class FollowPrepare(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    action: Literal['prepare']
    kind: Literal['person', 'pet']
    revision: str = Field(min_length=1, max_length=64)


class FollowChoose(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    action: Literal['choose']
    snapshot: str = Field(pattern=r'^[a-f0-9]{32}$')
    choice: int = Field(ge=1, le=10)


class FollowStop(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    action: Literal['stop']
    revision: str = Field(min_length=1, max_length=64)


FollowCommand = Annotated[FollowPrepare | FollowChoose | FollowStop, Field(discriminator='action')]


@dataclass(frozen=True)
class FollowSnapshot:
    token: str
    revision: str
    session: str
    stamp: float
    expires: float
    window: tuple
    choices: tuple


class CameraFollowPicker:
    TTL = 8

    def __init__(self, director, sample, intent, change_mode, *, clock=time.monotonic):
        self.director = director
        self.sample = sample
        self.intent = intent
        self.change_mode = change_mode
        self.clock = clock
        self.frame = None

    def _change(self, mode):
        self.frame = None
        self.change_mode(mode)
        actual, revision = self.intent()
        if actual != mode:
            raise ValueError('camera_intent_changed')
        self.director.apply_mode(actual, revision)
        return dict(mode=actual, intent_revision=revision)

    def _subjects(self, sample):
        from .stable_tracks import valid_box

        if self.director.mode == 'pet_target' and not self.director._pets_current(sample):
            return []
        subjects = self.director._subjects(sample)
        if not isinstance(subjects, list) or len(subjects) > 10:
            return []
        tracks = [row for row in subjects if isinstance(row, dict) and type(row.get('id')) in (int, str)]
        if len({row['id'] for row in tracks}) != len(tracks):
            return []
        return [row for row in tracks if valid_box(row.get('box')) and row['box']['confidence'] >= .65
                and self.director.follow_key(row) is not None]

    def _window(self, sample):
        view = sample.get('_patrol_view')
        return (sample.get('_follow_motion'), view[0] if view is not None else None,
                self.director._camera_window(sample))

    @staticmethod
    def _jpeg(sample):
        jpeg = sample.get('_follow_jpeg')
        encoded = sample.get('frame')
        if jpeg is None and isinstance(encoded, str) and len(encoded) <= 2_666_668:
            try:
                jpeg = base64.b64decode(encoded, validate=True)
            except ValueError:
                return None
        if isinstance(jpeg, bytes) and len(jpeg) <= 2_000_000 and jpeg.startswith(b'\xff\xd8') and jpeg.endswith(b'\xff\xd9'):
            return base64.b64encode(jpeg).decode('ascii')
        return None

    def snapshot(self):
        mode, revision = self.intent()
        now, sample = self.clock(), self.sample()
        if mode not in TARGET_MODES or self.director.intent != (mode, revision) or self.director.target_session is not None:
            raise ValueError('follow_target_unavailable')
        self.frame = None
        waiting = dict(status='waiting', intent_revision=revision)
        if not self.director.target_frame_current(sample, now):
            return waiting
        subjects = sorted(self._subjects(sample), key=lambda row: (row['box']['x'], row['box']['y']))
        jpeg = self._jpeg(sample) if subjects else None
        if jpeg is None:
            return waiting
        frame = FollowSnapshot(secrets.token_hex(16), revision, sample['session_id'], sample['_local_received'],
                               now + self.TTL, self._window(sample),
                               tuple((self.director.follow_key(row), row.get('species'),
                                      self.director.identity(row)) for row in subjects))
        self.frame = frame
        choices = [dict(choice=index, species=row.get('species'), box={key: row['box'][key] for key in ('x', 'y', 'w', 'h')})
                   for index, row in enumerate(subjects, 1)]
        return dict(status='ready', snapshot=frame.token, intent_revision=revision, ttl_ms=self.TTL * 1000,
                    jpeg=jpeg, choices=choices)

    def command(self, request):
        mode, revision = self.intent()
        now, sample = self.clock(), self.sample()
        if isinstance(request, FollowPrepare):
            if request.revision != revision or not self.director.target_frame_current(sample, now, stable=False):
                raise ValueError('follow_target_unavailable')
            return self._change('pet_target' if request.kind == 'pet' else 'person_target')
        if isinstance(request, FollowStop):
            if request.revision != revision or mode not in TARGET_MODES:
                raise ValueError('camera_intent_changed')
            return self._change('hold')
        frame = self.frame
        if (not frame or request.snapshot != frame.token or request.choice > len(frame.choices)
                or mode not in TARGET_MODES or self.director.intent != (mode, revision)
                or frame.revision != revision or now >= frame.expires or self.director.target_session is not None
                or not self.director.target_frame_current(sample, now)
                or sample['session_id'] != frame.session or sample['_local_received'] < frame.stamp
                or self._window(sample) != frame.window):
            raise ValueError('follow_target_changed')
        key, species, identity = frame.choices[request.choice - 1]
        matches = [row for row in self._subjects(sample) if self.director.follow_key(row) == key and row.get('species') == species
                   and (identity is None or self.director.identity(row) == identity)]
        if len(matches) != 1:
            raise ValueError('follow_target_changed')
        self.frame = None
        self.director.select_target(sample, matches[0], now)
        return dict(mode=mode, intent_revision=revision, status='selected')
