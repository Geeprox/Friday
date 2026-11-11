"""Durable camera direction preferences, never shutter or motor authority.

A survey is a single live window. Its restart preference is hold, so a lost
connection/process cannot silently repeat an interrupted sweep.
"""

from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat


TARGET_MODES = {'person_target', 'pet_target'}
PET_MODES = {'pets', 'pet_target'}
CAMERA_MODES = {'auto', 'pets', 'hold', 'survey'} | TARGET_MODES


def camera_intent_readback(snapshot, *, mode, revision, status):
    """切换意图后，旧画面的巡视结果和计数不能跟到新一轮。"""
    report = dict(snapshot, mode=mode, intent_status=status)
    if snapshot.get('intent_revision') != revision or snapshot.get('mode') != mode:
        held = mode == 'hold'
        report.update(intent_revision=revision, phase='held' if held else 'waiting_video',
                      reason={'hold': '停留当前视角', 'survey': '准备巡视'}.get(mode, '等待画面'),
                      view_usable=False, target_locked=False, survey_attempts=0,
                      survey_confirmed_moves=0, survey_total_steps=None, survey_result=None,
                      patrol_view_state='idle')
    return report


def finish_camera_survey(report):
    """只用已确认的视角数结束本轮；转向请求本身不计入进度。"""
    total = report.get('survey_total_steps')
    complete = type(total) is int and total > 0 and report.get('survey_confirmed_moves') == total
    return dict(report, phase='survey_finished', view_usable=False, target_locked=False,
                survey_result='complete' if complete else 'interrupted',
                reason='本轮巡视完成' if complete else '本轮巡视未完成')


@dataclass(frozen=True)
class CameraIntent:
    mode: str
    revision: str
    status: str


class CameraIntentStore:
    def __init__(self, directory, selected):
        self.directory = Path(directory)
        self.camera = selected.get('id')
        did = (selected.get('device') or {}).get('did')
        region = selected.get('region', 'cn')
        if (not isinstance(self.camera, str) or not re.fullmatch(r'cam_[a-f0-9]{10}', self.camera)
                or not isinstance(did, str) or not 1 <= len(did) <= 256
                or not isinstance(region, str) or not re.fullmatch(r'[a-z0-9_-]{1,16}', region)):
            raise ValueError('invalid_camera_intent_source')
        self.binding = hashlib.sha256(json.dumps([self.camera, did, region]).encode()).hexdigest()

    @staticmethod
    def _private(info, *, directory=False):
        valid_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        if (not valid_type or info.st_uid != os.getuid() or info.st_mode & 0o077
                or not directory and (info.st_nlink != 1 or info.st_size > 4096)):
            raise ValueError('camera_intent_private_path_required')

    def _directory(self, *, create=False):
        if create:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            self._private(os.fstat(fd), directory=True)
            return fd
        except BaseException:
            os.close(fd)
            raise

    @classmethod
    def _read_fd(cls, directory):
        fd = os.open('director-intent.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=directory)
        try:
            cls._private(os.fstat(fd))
            return fd
        except BaseException:
            os.close(fd)
            raise

    @staticmethod
    def _unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate_camera_intent_field')
            result[key] = value
        return result

    def load(self, *, default='auto'):
        if default not in {'auto', 'hold'}:
            raise ValueError('invalid_camera_mode')
        try:
            directory = self._directory()
        except FileNotFoundError:
            return CameraIntent(default, 'initial', 'default')
        except (OSError, ValueError):
            return CameraIntent('hold', 'unavailable', 'unavailable')
        try:
            try:
                fd = self._read_fd(directory)
            except FileNotFoundError:
                return CameraIntent(default, 'initial', 'default')
            with os.fdopen(fd) as stream:
                value = json.load(stream, object_pairs_hook=self._unique)
            if (not isinstance(value, dict)
                    or set(value) != {'version', 'camera', 'binding', 'mode', 'revision'}
                    or type(value['version']) is not int or value['version'] != 1
                    or value['camera'] != self.camera or value['binding'] != self.binding
                    or value['mode'] not in ('auto', 'pets', 'hold')
                    or not isinstance(value['revision'], str)
                    or not re.fullmatch(r'[a-f0-9]{32}', value['revision'])):
                raise ValueError('invalid_camera_intent')
            return CameraIntent(value['mode'], value['revision'], 'saved')
        except (OSError, ValueError, TypeError, RecursionError):
            return CameraIntent('hold', 'unavailable', 'unavailable')
        finally:
            os.close(directory)

    def save(self, mode):
        if mode not in CAMERA_MODES:
            raise ValueError('invalid_camera_mode')
        revision = secrets.token_hex(16)
        value = dict(version=1, camera=self.camera, binding=self.binding,
                     mode='hold' if mode == 'survey' or mode in TARGET_MODES else mode, revision=revision)
        directory = self._directory(create=True)
        lock = None
        temporary = '.director-intent-' + revision + '.tmp'
        try:
            lock = os.open('director-intent.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                           0o600, dir_fd=directory)
            self._private(os.fstat(lock))
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # A corrupt owned record can be replaced by a new explicit choice;
            # a symlink, foreign/shared file or oversized unknown file cannot.
            try:
                existing = self._read_fd(directory)
            except FileNotFoundError:
                pass
            else:
                os.close(existing)
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                         0o600, dir_fd=directory)
            with os.fdopen(fd, 'w') as stream:
                json.dump(value, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, 'director-intent.json', src_dir_fd=directory,
                       dst_dir_fd=directory)
            os.fsync(directory)
            return CameraIntent(mode, revision, 'saved')
        finally:
            try:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
            finally:
                try:
                    if lock is not None:
                        os.close(lock)
                finally:
                    os.close(directory)
