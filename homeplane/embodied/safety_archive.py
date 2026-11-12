"""Metadata-only bridge from a captured safety event to reviewed NAS references."""

import math
import re
from datetime import UTC, datetime, timedelta

from homeplane.archive_contracts import ArchiveResolveRequest

from .rapid_review import MAX_FRAME_GAP
from .safety_recording import SafetyRecorder
from .sound_review import KEYFRAME_LOOKBACK, SOUND_SEARCH_MAX_AGE
from .temporal_model import WINDOW_SPAN
from .temporal_review import MAX_CORROBORATION_SECONDS, POSTURE_CORROBORATION_SECONDS


REPLAY_KINDS = {'fall', 'visual_review', 'sound_review'}


def _camera_source(event):
    camera, source = event.get('camera'), event.get('camera_source_context')
    if (isinstance(camera, str) and re.fullmatch(r'cam_[a-f0-9]{10}', camera)
            and isinstance(source, str) and re.fullmatch(r'[a-f0-9]{64}', source)):
        return camera, source
    return None


def _time(value):
    return type(value) in (int, float) and math.isfinite(value)


def _recording_frame(event, ordinal, stamp):
    """按同一条留档的采集时序校验关键帧，回看请求不接受客户端时间。"""
    captured = event.get('captured_at')
    if type(ordinal) is not int or ordinal < 0 or not (_time(stamp) and _time(captured)):
        return False
    if event['kind'] == 'fall':
        return captured - SafetyRecorder.PRE_WINDOW <= stamp <= captured + SafetyRecorder.POST_WINDOW
    if event['kind'] == 'visual_review':
        if event.get('basis') == 'same_pair_rapid_window':
            lookback = 3 * MAX_FRAME_GAP
        elif event.get('visual_kind') == 'prone_posture':
            lookback = POSTURE_CORROBORATION_SECONDS + WINDOW_SPAN
        else:
            lookback = MAX_CORROBORATION_SECONDS + WINDOW_SPAN
        return captured - lookback <= stamp <= captured
    views = event.get('sound_search_views')
    if isinstance(views, list) and any(isinstance(view, dict) and type(view.get('ordinal')) is int
            and view['ordinal'] == ordinal and type(view.get('step')) is int and 1 <= view['step'] <= 3
            for view in views):
        return captured < stamp <= captured + SOUND_SEARCH_MAX_AGE
    return ordinal < 3 and captured - KEYFRAME_LOOKBACK <= stamp <= captured


class SafetyArchive:
    def __init__(self, store, archive):
        self.store, self.archive = store, archive

    def events(self):
        events = self.store.events()
        for event in events:
            if event.get('simulated') is False and event.get('kind') in REPLAY_KINDS and _camera_source(event):
                for frame in event['frames']:
                    if _recording_frame(event, frame['ordinal'], frame['at']):
                        frame['recording_anchor'] = True
        return events

    def references(self, event_id, ordinal=None):
        event, frame_at = self.store.recording_target(event_id, ordinal)

        def answer(code, recordings=()):
            value = dict(event_id=event_id, code=code, recordings=list(recordings),
                         source_scan_attempted=False, media_read_attempted=False)
            if ordinal is not None:
                value['frame_ordinal'] = ordinal
            return value

        if event.get('simulated') is not False or event.get('kind') not in REPLAY_KINDS:
            return answer('event_recording_not_applicable')
        camera_source = _camera_source(event)
        if camera_source is None:
            return answer('event_source_unverified')
        camera_id, source = camera_source
        captured = event.get('captured_at')
        if not _time(captured):
            return answer('event_time_unavailable')
        if ordinal is not None:
            if not _recording_frame(event, ordinal, frame_at):
                return answer('event_frame_unavailable')
            captured = frame_at
        try:
            stamp = datetime.fromtimestamp(captured, UTC)
            span = dict(start=stamp-timedelta(seconds=15), end=stamp+timedelta(seconds=15))
        except (ValueError, OverflowError, OSError):
            return answer('event_time_unavailable')
        if self.archive is None or getattr(self.archive, 'closed', False):
            return answer('archive_not_configured')
        context = self.archive.context()
        if context.get('simulated') or context.get('closed') or not context.get('configured'):
            return answer('archive_not_configured')
        candidates = [row for row in context.get('cameras', [])[:16]
                      if row.get('capture_camera_id') == camera_id and row.get('simulated') is False]
        if not candidates:
            return answer('archive_capture_mapping_required')
        matched = [row for row in candidates if row.get('source_binding_current') is True
                   and row.get('capture_binding_current') is True
                   and row.get('capture_source_context') == source]
        if not matched:
            return answer('archive_capture_mapping_changed')
        references = []
        for camera in matched:
            uncertainty = camera.get('clock_uncertainty_seconds')
            if (camera.get('timing_reviewed') is not True
                    or camera.get('completion_convention') != 'closed_files_operator_reviewed'
                    or type(uncertainty) not in (int, float) or not math.isfinite(uncertainty)
                    or not 0 <= uncertainty <= 60):
                continue
            request = ArchiveResolveRequest(expected_context=context['fingerprint'], camera_id=camera['id'],
                                            time_range=span)
            references.append(dict(event_id=event_id, camera_name=camera['name'], anchor_at=stamp.isoformat(),
                                   request=request.model_dump(mode='json'), playback_verified=False))
        return answer('configured_camera_time_lookup' if references else 'archive_recording_clock_unreviewed',
                      references)
