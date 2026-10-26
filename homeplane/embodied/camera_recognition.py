"""One bounded native-vision/tracking session per secondary camera.

Uses the same body, hand, attention and face pipeline as the game camera. No
frames are recorded, and no home floor-plan coordinates are invented here.
"""

import asyncio
import base64
import copy
import json
import secrets
import time

from .paths import MODELS, VISION, camera_storage
from .room_engine import DEFAULT, Room
from .stable_tracks import StableTracks
from .camera_director import visible_scene
from homeplane.people.live import FaceIdentityPipeline, analyze


class CameraRecognition:
    def __init__(self, camera, store):
        self.camera = camera
        self.session = secrets.token_hex(12)
        self.identities = FaceIdentityPipeline(store, camera, self.session, MODELS)
        self.tracks = StableTracks()
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
        self.last = None

    async def start(self):
        self.vision = await asyncio.create_subprocess_exec(str(VISION), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=6_000_000)

    async def observe(self, packet):
        motion_generation = self.motion_generation
        if not 0 <= time.time()-packet['received_at'] <= 1.5:
            self.identities.discard()
            self.last = None
            return None
        began = time.monotonic()
        self.vision.stdin.write((json.dumps(dict(jpeg=packet['jpeg'], face_jpeg=packet.get('face_jpeg'),
            hand_interval_ms=100, attention_enabled=True))+'\n').encode())
        await self.vision.stdin.drain()
        detection = json.loads(await asyncio.wait_for(self.vision.stdout.readline(), 12))
        if detection.get('error'):
            raise ValueError('camera_vision_unavailable')
        now = time.monotonic()
        vision_done = now
        picture = base64.b64decode(packet['jpeg'])
        if self.room is None:
            folder = camera_storage(self.camera)
            config_path, reference_path = folder/'room-calibration.json', folder/'room-reference.jpg'
            config = json.loads(config_path.read_text()) if config_path.exists() else copy.deepcopy(DEFAULT)
            if not config_path.exists():config.update(mode='unconfigured',furniture=[])
            reference = reference_path.read_bytes() if reference_path.exists() else picture
            self.room = Room(config, reference)
            if not reference_path.exists():
                reference_path.write_bytes(picture)
                config_path.write_text(json.dumps(config,ensure_ascii=False))
        if now < self.motion_until:
            self.room.expect_motion(now, self.motion_until-now)
        await asyncio.to_thread(self.room.register, picture, [{'box':b} for b in detection.get('boxes',[])], now)
        moving = now < self.room.command_until or self.room.reg_info.get('status') in {'moving','settling'}
        people = self.tracks.update(detection, now, transform=self.room.registration, camera_moving=moving)
        hands = detection.get('hands', [])
        layout = await asyncio.to_thread(self.room.update, picture, people, hands, now,
                                        packet['width'], packet['height'], [], True)
        registration = layout['registration']
        moving = moving or registration['status'] in {'moving', 'settling'}
        layout_done = time.monotonic()
        rows = await analyze(self.identities, base64.b64decode(packet.get('face_jpeg') or packet['jpeg']),
                             people, detection.get('faces', []), moving=moving,
                             sample=bool(packet.get('face_jpeg')) or max(packet.get('native_size') or [0])<=1920,
                             captured_at=time.monotonic()-(time.time()-packet['received_at']))
        self.timing = dict(vision_ms=round((vision_done-began)*1000),
            layout_ms=round((layout_done-vision_done)*1000),
            identity_ms=round((time.monotonic()-layout_done)*1000),
            pipeline_ms=round((time.time()-packet['received_at'])*1000))
        scene_visible = await asyncio.to_thread(visible_scene, picture)
        if motion_generation != self.motion_generation or not 0 <= time.time()-packet['received_at'] <= 1.5:
            self.identities.discard()
            self.last = None
            return None
        by_track = {r['track']:r for r in rows}
        for person in people:
            person['identity'] = by_track.get(str(person['id']), dict(state='unknown', person_id=None))
        self.last = dict(camera=self.camera, session=self.session, people=people, hands=hands,
                         faces=detection.get('faces', []), registration=registration, now=now,
                         captured_mono=time.monotonic()-(time.time()-packet['received_at']),
                         safety_jpeg=picture, aspect=packet['width'] / max(1, packet['height']),
                         scene_visible=scene_visible)
        return self.last

    def identity_status(self):
        return dict(camera=self.camera, session=self.session, reason=self.identities.reason, diagnostics=self.identities.diagnostics(),
                    tracks=self.identities.public())

    async def close(self):
        self.identities.close()
        if self.vision and self.vision.returncode is None:
            self.vision.terminate()
            try:
                await asyncio.wait_for(self.vision.wait(), 3)
            except asyncio.TimeoutError:
                self.vision.kill()
                await self.vision.wait()
