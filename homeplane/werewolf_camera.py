"""Advisory party hints from the current perception owner's public observation.

This module neither opens a camera nor infers a seat, identity, role or vote.
"""

import base64
import binascii
import math
import re
from typing import NamedTuple

from .embodied.pose_rules import body_track_revision


class CameraTrack(NamedTuple):
    camera: str
    session: str
    track: int
    track_revision: int


def unit(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1


def point(value):
    return (isinstance(value, dict) and all(unit(value.get(k)) for k in ('x', 'y', 'confidence'))
            and value['confidence'] >= .5)


def observation(report):
    if not isinstance(report, dict):
        return None
    room = report.get('room')
    age = report.get('frame_age_ms')
    camera, session = report.get('camera_id'), report.get('session_id')
    if (report.get('status') != 'live' or report.get('tracking_current') is not True
            or report.get('scene_visible') is not True
            or type(age) not in (int, float) or not math.isfinite(age) or not 0 <= age < 1000
            or not isinstance(camera, str) or not re.fullmatch(r'cam_[a-f0-9]{10}', camera)
            or not isinstance(session, str) or not 1 <= len(session) <= 128
            or not isinstance(room, dict) or not isinstance(room.get('registration'), dict)
            or room['registration'].get('status') != 'locked'):
        return None
    people = report.get('people')
    seats, pointing = room.get('seats'), room.get('pointing')
    if (not isinstance(people, list) or len(people) > 32
            or not isinstance(seats, list) or len(seats) > 128
            or not isinstance(pointing, list) or len(pointing) > 32):
        return None
    tracks = {}
    for person in people:
        if not isinstance(person, dict):
            return None
        track, box = person.get('id'), person.get('box')
        if (type(track) is not int or track <= 0 or track in tracks
                or not isinstance(box, dict) or not all(unit(box.get(k)) for k in ('x', 'y', 'w', 'h', 'confidence'))
                or box['w'] <= 0 or box['h'] <= 0 or box['confidence'] < .4
                or box['x'] + box['w'] > 1.000001 or box['y'] + box['h'] > 1.000001):
            return None
        tracks[track] = person
    current = {track: CameraTrack(camera, session, track, revision)
               for track, person in tracks.items()
               if (revision := body_track_revision(person)) is not None}
    return dict(camera=camera, session=session, age_ms=age, people=tracks, tracks=current,
                seats=seats, seat_mode=room.get('seat_mode'), pointing=pointing)


def picture(report):
    """Project one atomic worker frame; no identity, home state or transcripts."""
    sample = observation(report)
    if sample is None:
        return None
    width, height, jpeg = report.get('width'), report.get('height'), report.get('frame')
    if (type(width) is not int or type(height) is not int
            or not 1 <= width <= 4096 or not 1 <= height <= 4096 or width * height > 8_294_400
            or not isinstance(jpeg, str) or not 4 <= len(jpeg) <= 2_700_000):
        return None
    try:
        raw = base64.b64decode(jpeg, validate=True)
    except (ValueError, binascii.Error):
        return None
    if not raw.startswith(b'\xff\xd8') or not raw.endswith(b'\xff\xd9'):
        return None
    return dict(camera=sample['camera'], session=sample['session'], age_ms=sample['age_ms'],
                width=width, height=height, jpeg=jpeg,
                people=[dict(track=track, track_revision=ref.track_revision,
                             box={k: sample['people'][track]['box'][k] for k in ('x', 'y', 'w', 'h')})
                        for track, ref in sample['tracks'].items()])


def _seat_tracks(sample):
    """把空间视图里的位置对应到实际入镜的人体。"""
    occupied = {}
    if sample['seat_mode'] == 'manual':
        for track, person in sample['people'].items():
            if type(seat := person.get('seat')) is int:
                occupied.setdefault(seat, []).append(track)
    else:
        for seat in sample['seats']:
            if isinstance(seat, dict) and type(seat.get('id')) is int and seat.get('active') is True:
                occupied.setdefault(seat['id'], []).append(seat.get('track'))
    return occupied


def hints(sample, bindings, players):
    mapped = {ref.track: seat for seat, ref in bindings.items()
              if sample['tracks'].get(ref.track) == ref and seat in players and players[seat].alive}
    occupied = _seat_tracks(sample)
    result = []
    for track, seat in mapped.items():
        joints = sample['people'][track].get('joints')
        joints = joints if isinstance(joints, dict) else {}
        reference = next((joints[k] for k in ('nose', 'neck') if point(joints.get(k))), None)
        raised = bool(reference and any(point(joints.get(side + 'Wrist'))
            and joints[side + 'Wrist']['y'] < reference['y'] - .05 for side in ('left', 'right')))
        targets = set()
        for ray in sample['pointing']:
            if (not isinstance(ray, dict) or type(ray.get('person_id')) is not int
                    or ray['person_id'] != track or ray.get('confirmed') is not True
                    or ray.get('ambiguous') is not False):
                continue
            if type(ray.get('target')) is not int:
                continue
            matches = occupied.get(ray['target'], [])
            if len(matches) == 1 and type(matches[0]) is int:
                target = mapped.get(matches[0])
                if target is not None:
                    targets.add(target)
        result.append(dict(seat=seat, hand_raised=raised, pointing_at=next(iter(targets)) if len(targets) == 1 else None,
                           advisory=True, vote_submitted=False))
    return result
