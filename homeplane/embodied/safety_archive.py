"""Metadata-only bridge from a captured safety event to reviewed NAS references."""

import math
import re
from datetime import UTC, datetime, timedelta

from homeplane.archive_contracts import ArchiveResolveRequest


class SafetyArchive:
    def __init__(self, store, archive):
        self.store, self.archive = store, archive

    def references(self, event_id, ordinal=None):
        event, frame_at = self.store.recording_target(event_id, ordinal)

        def answer(code, recordings=()):
            value = dict(event_id=event_id, code=code, recordings=list(recordings),
                         source_scan_attempted=False, media_read_attempted=False)
            if ordinal is not None:
                value['frame_ordinal'] = ordinal
            return value

        if event.get('simulated') is not False or event.get('kind') not in {'fall', 'visual_review', 'sound_review'}:
            return answer('event_recording_not_applicable')
        source = event.get('camera_source_context')
        camera_id = event.get('camera')
        if (not isinstance(source, str) or not re.fullmatch(r'[a-f0-9]{64}', source)
                or not isinstance(camera_id, str) or not re.fullmatch(r'cam_[a-f0-9]{10}', camera_id)):
            return answer('event_source_unverified')
        captured = event.get('captured_at')
        if type(captured) not in (int, float) or not math.isfinite(captured):
            return answer('event_time_unavailable')
        if ordinal is not None:
            views = event.get('sound_search_views')
            if (type(ordinal) is not int or event['kind'] != 'sound_review' or not isinstance(views, list) or
                    not any(isinstance(view, dict) and type(view.get('ordinal')) is int
                            and view['ordinal'] == ordinal and type(view.get('step')) is int
                            and 1 <= view['step'] <= 3 for view in views)):
                return answer('event_frame_unavailable')
            if type(frame_at) not in (int, float) or not math.isfinite(frame_at) or frame_at <= captured:
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
            references.append(dict(event_id=event_id, camera_name=camera['name'],
                                   request=request.model_dump(mode='json'), playback_verified=False))
        return answer('configured_camera_time_lookup' if references else 'archive_recording_clock_unreviewed',
                      references)
