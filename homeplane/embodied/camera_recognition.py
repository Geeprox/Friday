"""One bounded native-vision/tracking session per secondary camera.

Uses the same body, hand, attention and face pipeline as the game camera. No
frames are recorded, and no home floor-plan coordinates are invented here.
"""

import asyncio
import base64
import secrets
import time

from .paths import MODELS, VISION, camera_storage
from .room_session import RoomSession
from .room_protocol import unavailable_layout
from .stable_tracks import StableTracks
from .pet_tracks import PetTracks
from .vision_pipe import request_vision
from homeplane.people.live import analyze, close_identity
from homeplane.people.identity_session import IdentitySession


class CameraRecognition:
    def __init__(self, camera, store):
        self.camera = camera
        self.session = secrets.token_hex(12)
        self.identities = IdentitySession(store, camera, self.session, MODELS)
        self.tracks = StableTracks()
        self.pet_tracks = PetTracks()
        self.animals_enabled = False
        self.animals_intent = None
        self.room = None
        self.vision = None
        self.last = None
        self.timing = {}
        self.motion_generation = 0
        self.motion_until = 0

    def mark_motion(self, now):
        self.motion_generation += 1
        self.motion_until = now + 2
        self.identities.discard()
        self.pet_tracks.clear()
        self.last = None
        if self.room is not None:self.room.expect_motion(now, 2)

    async def start(self):
        self.vision = await asyncio.create_subprocess_exec(str(VISION), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=6_000_000)

    async def observe(self, packet, *, source_current=None):
        motion_generation = self.motion_generation
        def current():
            return (motion_generation == self.motion_generation
                    and (source_current is None or source_current())
                    and 0 <= time.time()-packet['received_at'] <= 1.5)

        if not current():
            self.identities.discard()
            self.last = None
            return None
        began = time.monotonic()
        animal_intent = self.animals_intent if self.animals_enabled else None
        detection = await request_vision(self.vision, dict(jpeg=packet['jpeg'], face_jpeg=packet.get('face_jpeg'),
            hand_interval_ms=100, attention_enabled=True, animals_enabled=animal_intent is not None))
        if not current():
            self.identities.discard()
            self.last = None
            return None
        now = time.monotonic()
        vision_done = now
        picture = base64.b64decode(packet['jpeg'])
        if self.room is None:
            self.room = RoomSession(camera_storage(self.camera))
        if now < self.motion_until:
            self.room.expect_motion(now, self.motion_until-now)
        masks = [{'box': b} for b in detection.get('boxes', [])]
        masks.extend({'box': row['box']} for row in PetTracks.read_rows(detection) or [])
        registered = await self.room.register(picture, masks, now, current=current,
            active=lambda: motion_generation == self.motion_generation
                and (source_current is None or source_current()))
        if not current():
            self.identities.discard()
            self.last = None
            return None
        moving = (not registered or now < self.motion_until or now < self.room.command_until
                  or self.room.reg_info.get('status') in {'moving','settling'})
        people = self.tracks.update(detection, now, transform=self.room.registration, camera_moving=moving)
        animals = self.pet_tracks.update(detection, now, intent=animal_intent,
                                        transform=self.room.registration, camera_moving=moving)
        hands = detection.get('hands', [])
        layout = (await self.room.update(picture, people, hands, now,
                                        packet['width'], packet['height'], [], current=current)) if registered else None
        if layout is None:
            self.tracks.invalidate();self.pet_tracks.clear();self.identities.discard()
            people,hands,animals=[],[],[]
            layout=unavailable_layout()
        if not current():
            self.identities.discard()
            self.last = None
            return None
        registration = layout['registration']
        moving = moving or registration['status'] in {'moving', 'settling'}
        if moving:
            self.pet_tracks.clear()
            animals = []
        layout_done = time.monotonic()
        rows = await analyze(self.identities, base64.b64decode(packet.get('face_jpeg') or packet['jpeg']),
                             people, detection.get('faces', []), moving=moving,
                             sample=bool(packet.get('face_jpeg')) or max(packet.get('native_size') or [0])<=1920,
                             captured_at=time.monotonic()-(time.time()-packet['received_at']))
        if not current():
            self.identities.discard()
            self.last = None
            return None
        self.timing = dict(vision_ms=round((vision_done-began)*1000),
            animal_ms=detection.get('animal_ms', 0),
            layout_ms=round((layout_done-vision_done)*1000),
            identity_ms=round((time.monotonic()-layout_done)*1000),
            pipeline_ms=round((time.time()-packet['received_at'])*1000))
        scene_visible = self.room.scene_visible
        if not current():
            self.identities.discard()
            self.last = None
            return None
        by_track = {r['track']:r for r in rows}
        for person in people:
            person['identity'] = by_track.get(str(person['id']), dict(state='unknown', person_id=None))
        self.last = dict(camera=self.camera, session=self.session, people=people, hands=hands,
                         animals=animals, animals_current=self.pet_tracks.current,
                         animals_intent=animal_intent, pet_tracking=self.pet_tracks.public(),
                         tracking_current=self.tracks.input_valid,
                         room_service=self.room.status(),
                         camera_source_context=packet.get('camera_source_context'),
                         camera_source_name=packet.get('camera_source_name'),
                         faces=detection.get('faces', []), registration=registration, now=now,
                         captured_mono=time.monotonic()-(time.time()-packet['received_at']),
                         safety_jpeg=picture, aspect=packet['width'] / max(1, packet['height']),
                         scene_visible=scene_visible)
        return self.last

    def identity_status(self):
        return dict(camera=self.camera, session=self.session, reason=self.identities.reason, diagnostics=self.identities.diagnostics(),
                    tracks=self.identities.public())

    async def close(self):
        try:
            identity_closed = await close_identity(self.identities)
        finally:
            try:
                if self.room is not None:await self.room.close()
            finally:
                if self.vision and self.vision.returncode is None:
                    try:self.vision.terminate()
                    except ProcessLookupError:pass
                    try:
                        await asyncio.wait_for(self.vision.wait(), 3)
                    except asyncio.TimeoutError:
                        try:self.vision.kill()
                        except ProcessLookupError:pass
                        await self.vision.wait()
        return identity_closed
