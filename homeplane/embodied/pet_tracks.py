"""Current cat/dog rectangles in a separate track namespace; never people or identity.

The native request is opt-in. Unsupported/old workers, malformed detections,
camera movement and a changed user intent cannot supply a follow target.
"""

from .stable_tracks import StableTracks, valid_box


class PetTracks:
    def __init__(self):
        self.trackers = {}
        self.generation = 0
        self.intent = None
        self.current = False
        self.supported = False
        self.status = 'disabled'

    def clear(self):
        if self.trackers:
            # 追踪器重建会复用局部编号，旧画面上的宠物不能与新编号自动关联。
            self.generation += 1
        self.trackers.clear()
        self.intent = None
        self.current = False

    def public(self):
        return dict(status=self.status, supported=self.supported, current=self.current,
                    species=['cat', 'dog'] if self.supported else [], identity=False)

    @staticmethod
    def read_rows(detection):
        if (not isinstance(detection, dict) or detection.get('animals_supported') is not True
                or detection.get('animals_status') != 'ready'):
            return None
        rows = detection.get('animals')
        if (not isinstance(rows, list) or len(rows) > 10
                or any(not isinstance(row, dict) or row.get('species') not in ('cat', 'dog')
                       or not valid_box(row.get('box')) or row['box']['confidence'] < .75 for row in rows)):
            return None
        return rows

    def update(self, detection, now, *, intent=None, transform=None, camera_moving=False):
        self.current = False
        self.supported = isinstance(detection, dict) and detection.get('animals_supported') is True
        self.status = 'disabled' if intent is None else 'unavailable'
        if (not self.supported or not isinstance(intent, str) or not 1 <= len(intent) <= 64
                or detection.get('animals_status') != 'ready'):
            self.clear()
            return []
        rows = self.read_rows(detection)
        if rows is None:
            self.clear()
            return []
        if intent != self.intent:
            self.clear()
            self.intent = intent
        if not self.trackers:
            self.trackers = {species: StableTracks() for species in ('cat', 'dog')}
        result = []
        for species, tracker in self.trackers.items():
            boxes = [{key: row['box'][key] for key in ('x', 'y', 'w', 'h', 'confidence')}
                     for row in rows if row['species'] == species]
            tracks = tracker.update(dict(boxes=boxes, poses=[]), now, transform=transform,
                                    camera_moving=camera_moving,
                                    occluders=[row['box'] for row in rows if row['species'] != species])
            result.extend(dict(id=f"{species}:{self.generation}:{track['id']}", species=species,
                               box=dict(track['box']), motion=track['motion'],
                               tracking=dict(track['tracking'])) for track in tracks)
        self.current = all(tracker.input_valid for tracker in self.trackers.values())
        self.status = 'ready' if self.current else 'unavailable'
        return result if self.current else []
