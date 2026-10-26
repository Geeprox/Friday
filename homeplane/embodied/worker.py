"""Household perception owner; games and viewers consume shared camera sessions."""

import asyncio
import base64
import collections
import copy
import json
import os
import secrets
import struct
import time
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse

from homeplane.embodied.extensions import ActionBindings, RecognizerRegistry
from homeplane.embodied.home_service import HomeService
from homeplane.embodied.motion_games import MotionGames
from homeplane.embodied.page_recording import PageRecording
from homeplane.embodied.paths import (
    DATA,
    MODELS,
    PRIVATE,
    PYTHON,
    VISION,
    camera,
    camera_storage,
    capture_environment,
    configuration,
)
from homeplane.embodied.paths import (
    SOURCE as HERE,
)
from homeplane.embodied.plugins import configure
from homeplane.embodied.previews import PreviewPool, camera_is_open
from homeplane.embodied.ptz_control import CameraLease, LocalFollower, SearchPlanner, Transport
from homeplane.embodied.room_engine import DEFAULT, Room, project, validate
from homeplane.embodied.scene_detector import SceneDetector
from homeplane.embodied.camera_director import CameraDirector, visible_scene
from homeplane.embodied.frame_sampling import offer_frame
from homeplane.embodied.camera_wall import CameraWall
from homeplane.embodied.stable_tracks import StableTracks as Tracks
from homeplane.people.live import FaceIdentityPipeline, IdentityStore, analyze

os.umask(0o077)
KEY_PATH = PRIVATE / "viewer-key"
KEY = KEY_PATH.read_text().strip()
ALLOWED_IPS = set(configuration()["viewer_hosts"])
SELECTED = dict(camera())
wall = CameraWall(configuration()['cameras'])
primary_wall_epoch = 0
if os.environ.get("FRIDAY_CAMERA_OPEN")=="1":SELECTED["takeover"]=True
if os.environ.get("FRIDAY_BACKGROUND") == "1":
    SELECTED["takeover"] = True
interaction_enabled = os.environ.get("FRIDAY_INTERACTION_ENABLED", "1") == "1"
CAMERA_STORAGE = camera_storage()
SESSION = secrets.token_hex(12)
state = {
    "status": "stopped",
    "reason": None,
    "frame": None,
    "processed_frames": 0,
    "people": [],
    "hands": [],
    "room": None,
    "inference_ms": None,
    "source": "Friday / Miloco local SDK",
    "camera_id": SELECTED["id"],
    "session_id": SESSION,
    "simulated": False,
}
task = None
capture = None
vision = None
generation = 0
tracks = Tracks()
latest = None
last_frame = 0
last_jpeg = b""
last_preview = b""
last_preview_at = 0.0
changed = asyncio.Condition()
room = None
calibration_path = CAMERA_STORAGE / "room-calibration.json"
reference_path = CAMERA_STORAGE / "room-reference.jpg"
processed_times = collections.deque(maxlen=120)
captured_times = collections.deque(maxlen=120)
room_lock = asyncio.Lock()
camera_lease = None
camera_planner = None
motor_waiters = {}
motor_sequence = 0
motion_generation = 0
games = MotionGames()
recorder = PageRecording(DATA / "recordings")
record_lock = asyncio.Lock()
home = HomeService(SELECTED['id'])
from homeplane.embodied.interaction import InteractionEngine
from homeplane.embodied.interaction_fleet import InteractionFleet
interaction = InteractionEngine(SELECTED["id"], SESSION)
fleet = InteractionFleet()
secondary_homes = {}
identity_store = IdentityStore(PRIVATE / "identities.sqlite")
from homeplane.embodied.safety import SafetyRuntime, SafetyStore
safety = SafetyRuntime(SafetyStore(PRIVATE / "safety.sqlite"), labels=identity_store.labels)
identities = FaceIdentityPipeline(identity_store, SELECTED["id"], SESSION, MODELS)
async def dispatch_interactions(events):
    for event in events:
        async def dispatch(e=event):
            service = home if e['camera'] == SELECTED['id'] else secondary_homes.get(e['camera'])
            if not (interaction_enabled and service and service.armed and fleet.current(e,time.monotonic())):return
            from homeplane.embodied.bridge import command
            receipt = await command(None,e['action'],e['event_id'],target=e['target'],camera_id=e['camera'],
                gesture={k:e[k] for k in ('binding','revision','observed_mono')})
            action={'open':'打开','close':'关闭','on':'开启','off':'关闭'}.get(e['action'],'设备控制')
            outcome='设备状态已更新' if receipt.get('reached_target') else '指令已发送' if receipt.get('acknowledged') else '未执行'
            wall.notice(e['camera'],action+' · '+outcome)
            fleet.receipts.append(dict(camera=e['camera'],target=e['target'],action=e['action'],
                status=receipt.get('status','unavailable'),acknowledged=bool(receipt.get('acknowledged')),
                reached_target=bool(receipt.get('reached_target')),at=time.time()))
        home.spawn(dispatch())


def clear_camera(camera_id):
    safety.clear(camera_id)
    fleet.clear(camera_id)
    service=home if camera_id==SELECTED['id'] else secondary_homes.get(camera_id)
    if service:
        service.selected=False;service.epoch+=1;service.vad=[];service.pending_audio=None


def close_primary_view():
    global motion_generation
    motion_generation += 1
    state.update(status='shutter_closed',reason='privacy_shutter_closed',people=[],hands=[],frame=None)
    wall.clear(SELECTED['id'], primary_wall_epoch)
    clear_camera(SELECTED['id'])
    identities.discard()


async def secondary_observation(observation, process):
    camera_id=observation['camera']
    await safety.observe(camera_id, observation['session'], observation['people'], observation['now'],
        observation['captured_mono'], stable=observation.get('scene_visible', False)
            and observation['registration'].get('status') == 'locked',
        jpeg=observation.get('safety_jpeg', b''), aspect=observation.get('aspect', 16 / 9))
    wall.observe(camera_id,observation['session'],observation['people'],
        time.time()-(time.monotonic()-observation['captured_mono']),
        hands=observation['hands'],
        moving=observation['registration'].get('status') in {'moving','settling','lost','stale'})
    active=interaction_enabled
    events=fleet.update(camera_id,observation['session'],observation['people'],observation['hands'],
        observation['registration'],active,observation['now'])
    await dispatch_interactions(events)
    if camera_id not in secondary_homes:secondary_homes[camera_id]=HomeService(camera_id,voice_provider=home)
    service=secondary_homes[camera_id]
    if active and not service.enabled:await service.enable()
    service.armed=active
    item=fleet.cameras.get(camera_id,{})
    now = time.monotonic()
    service.audio_allowed=fleet.voice(camera_id, now, service.attention.candidate() is not None,
        people=len(observation['people']), session=observation['session'],
        speaking=bool(service.speaking and service.last_audio is not None and 0 <= now-service.last_audio <= .6),
        enabled=active and service.armed and 0 <= now-observation['now'] <= 1.5
            and not (PRIVATE/'voice-input-paused').exists())
    await service.observe(observation['people'],observation['faces'],observation['registration'],
        observation['now'],process,active,item.get('owner'),gestures_enabled=False)


async def secondary_audio(camera_id, packet):
    service=secondary_homes.get(camera_id)
    if service:
        service.audio_allowed = service.audio_allowed and fleet.voice_current(camera_id, time.monotonic())
        await service.audio(packet)


previews = PreviewPool(identity_store=identity_store, continuous=True, recognize=True, wall=wall,
    on_observation=secondary_observation,on_audio=secondary_audio,on_clear=clear_camera,
    focus=lambda cid:getattr(secondary_homes.get(cid),'camera_focus',None),
    occupied_elsewhere=lambda cid: (cid!=SELECTED['id'] and bool(view().get('people')))
        or any(other!=cid and previews.status(other)['state']=='live' and item.get('people',0)
               for other,item in previews.sessions.items()))
registry = RecognizerRegistry()
bindings = ActionBindings(registry)
configure(registry, bindings, games, home)


async def motor_step(direction, observed=None):
    global motor_sequence, motion_generation
    if direction not in ("left", "right", "up", "down"):
        raise ValueError("invalid_motor_direction")
    if state.get('status')=='shutter_closed':raise RuntimeError('privacy_shutter_closed')
    if (
        not capture
        or not capture.stdin
        or not camera_lease
        or not camera_lease.snapshot
        or camera_lease.snapshot.get("status") != "owned"
    ):
        raise RuntimeError("camera_not_owned")
    owned_capture, owned_lease = capture, camera_lease
    if not await camera_is_open(SELECTED):
        close_primary_view()
        raise RuntimeError('privacy_shutter_closed')
    if state.get('status')=='shutter_closed':raise RuntimeError('privacy_shutter_closed')
    if observed is not None and not 0 <= time.monotonic()-observed <= 1.5:
        raise RuntimeError('stale_input')
    if observed is not None and camera_planner and camera_planner.mode == 'hold':
        raise RuntimeError('stale_input')
    motor_sequence += 1
    request_id = motor_sequence
    future = asyncio.get_running_loop().create_future()
    motor_waiters[request_id] = future
    motion_generation += 1
    identities.discard()
    fleet.clear(SELECTED['id'])
    home.selected = False
    home.epoch += 1
    home.vad = []
    home.pending_audio = None
    correlation = secrets.token_hex(16)
    began = time.monotonic()
    home.runtime_log.record('embodied.command','start',correlation=correlation)
    try:
        async with room_lock:
            if room:
                room.expect_motion(time.monotonic(), 2)
        # The lock may have waited behind vision registration. Revalidate all
        # authority and freshness immediately before the irreversible send.
        if state.get('status')=='shutter_closed':raise RuntimeError('privacy_shutter_closed')
        if (capture is not owned_capture or camera_lease is not owned_lease
                or not camera_lease.snapshot or camera_lease.snapshot.get('status')!='owned'):
            raise RuntimeError('camera_not_owned')
        if observed is not None and (not 0 <= time.monotonic()-observed <= 1.5
                or camera_planner and camera_planner.mode=='hold'):
            raise RuntimeError('stale_input')
        capture.stdin.write(
            (
                json.dumps({"action": "motor_step", "direction": direction, "request_id": request_id}) + "\n"
            ).encode()
        )
        await capture.stdin.drain()
        result = await asyncio.wait_for(future, 3)
        if result.get("error_class") or result.get("native_code") != 0:
            raise RuntimeError("local_motor_rejected")
        state["local_motor"] = dict(result, at=time.time())
        home.runtime_log.record('embodied.command','result',correlation=correlation,
            elapsed_ms=round((time.monotonic()-began)*1000))
        return result
    except asyncio.CancelledError:
        home.runtime_log.record('embodied.command','result',correlation=correlation,
            result='cancelled',code='control_cancelled_after_send',elapsed_ms=round((time.monotonic()-began)*1000))
        raise
    except Exception:
        home.runtime_log.record('embodied.command','result',correlation=correlation,
            result='unavailable',code='runtime_error',elapsed_ms=round((time.monotonic()-began)*1000))
        raise
    finally:
        motor_waiters.pop(request_id, None)


async def camera_search():
    global camera_lease, camera_planner
    io = Transport()
    lease = CameraLease(io, open_privacy=os.environ.get("FRIDAY_CAMERA_OPEN")=="1", restore_native=False)
    camera_lease = lease
    try:
        await lease.acquire()
        planner = SearchPlanner(lease.presets, time.monotonic(), lease.active)
        if os.environ.get("FRIDAY_CAMERA_HOLD")=="1":planner.mode="hold"
        camera_planner = planner
        director = CameraDirector()
        checked = 0.0
        while True:
            now = time.monotonic()
            if now-checked >= 10:
                checked = now
                if not await camera_is_open(SELECTED):
                    close_primary_view()
                    return
            now = time.monotonic()
            sample = dict(state, _local_received=last_frame)
            elsewhere = any(item.get('people',0) and previews.status(cid)['state']=='live'
                            for cid,item in previews.sessions.items())
            direction = director.choose(sample, now, focus=getattr(home,'camera_focus',None),
                                        occupied_elsewhere=elsewhere) if planner.mode!='hold' else None
            if planner.mode == 'hold':director.hold()
            state['camera_control'] = dict(planner.public(lease.presets), **director.public())
            if direction is not None:
                try:
                    await motor_step(direction, sample['_local_received'])
                    director.sent(time.monotonic())
                    lease.event('local_follow_impulse', direction=direction)
                except RuntimeError as error:
                    # Capture connection is not ready during startup; retry only
                    # after a new fresh observation, with no queued motor commands.
                    state['camera_control']['phase'] = 'motor_unavailable'
                    if str(error) != 'stale_input':director.failed(time.monotonic())
            await asyncio.sleep(.3)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        state["camera_control"] = {
            "owner": "none",
            "phase": "unavailable",
            "reason": "接管失败",
            "error_class": type(error).__name__,
        }
    finally:
        camera_planner = None
        try:
            restored = await lease.release()
            state["camera_control"] = {
                "owner": "none",
                "phase": "restored" if restored else "restore_pending",
                "reason": "镜头已释放" if restored else "镜头待释放",
            }
        finally:
            await io.close()
            camera_lease = None


def rate(times):
    return (
        round((len(times) - 1) / (times[-1] - times[0]), 1) if len(times) > 1 and times[-1] > times[0] else 0
    )


async def notify():
    async with changed:
        changed.notify_all()


def view():
    result = dict(state)
    result["game"] = games.public
    result["recording"] = recorder.public()
    result["home"] = home.public()
    result['interactions'] = fleet.public(time.monotonic())
    result['voice_input'] = not (PRIVATE/'voice-input-paused').exists()
    result["frame_age_ms"] = (
        round((time.time() - state.get("received_at", time.time())) * 1000) if last_frame else None
    )
    if result['status'] == 'live' and result["frame_age_ms"] is not None and result["frame_age_ms"] > 1500:
        result.update(
            status="unavailable",
            reason="stream_stale",
            frame=None,
            people=[],
            hands=[],
            room=None,
            game=dict(games.public, status="paused", reason="实时画面已过期", players=[]),
        )
    if result['status'] != 'live' and result.get('camera_control'):
        result['camera_control'] = dict(result['camera_control'], view_usable=False, target_locked=False)
    result['camera_sessions'] = [dict(camera=SELECTED['id'],state=result['status'],recognition='running' if result['status']=='live' else 'waiting',frames=state['processed_frames']),
        *[previews.status(c['id']) for c in configuration()['cameras'] if c['id']!=SELECTED['id']]]
    return result


def authorized(client, key):
    return bool(client and client.host in ALLOWED_IPS and secrets.compare_digest(key or "", KEY))


async def reap(process):
    if process and process.returncode is None:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 3)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()


async def run_source(my_generation):
    global capture, vision, latest, last_frame, tracks, last_jpeg, room, primary_wall_epoch
    primary_wall_epoch = wall.begin(SELECTED['id'])
    wall_epoch = primary_wall_epoch
    identities.session = f"{SESSION}_{my_generation}_{secrets.token_hex(3)}"
    state["session_id"] = identities.session
    games.reset("off")
    home.armed = False
    home.selected = False
    home.epoch += 1
    home.vad = []
    home.pending_audio = None
    fleet.clear(SELECTED['id'])
    home.gesture.reset()
    tracks = Tracks()
    processed_times.clear()
    captured_times.clear()
    state.update(
        status="connecting",
        reason=None,
        frame=None,
        people=[],
        hands=[],
        room=None,
        game={"mode": games.mode, "status": "paused", "reason": "实时画面已过期", "players": []},
    )
    camera_task = None
    stage = "source_start"
    try:
        if os.environ.get('FRIDAY_CAMERA_OPEN') != '1' and not await camera_is_open(SELECTED):
            state.update(status='shutter_closed', reason='privacy_shutter_closed')
            await notify()
            return
        camera_task = asyncio.create_task(camera_search()) if SELECTED.get('takeover') else None
        # Preview may have been physically shuttered before this lease. Wait
        # for its read-back-confirmed takeover before asking the SDK for video.
        ready_by = time.monotonic() + 30
        while camera_task and not (
            camera_lease and camera_lease.snapshot and camera_lease.snapshot.get("status") == "owned"
        ):
            if camera_task.done() or time.monotonic() > ready_by:
                raise RuntimeError("camera_takeover_not_ready")
            await asyncio.sleep(0.1)
        if camera_lease and camera_lease.snapshot.get("before", {}).get("camera_power") is False:
            # Property ACK precedes the C700's physical privacy shutter. Allow
            # hardware to wake before starting the local SDK handshake.
            await asyncio.sleep(5)
        stage = 'shutter_readback'
        if not await camera_is_open(SELECTED):
            state.update(status='shutter_closed', reason='privacy_shutter_closed')
            await notify()
            return
        stage = 'source_start'
        capture = await asyncio.create_subprocess_exec(
            str(PYTHON),
            "-B",
            "-m",
            "homeplane.embodied.capture_worker",
            env=capture_environment(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=6_000_000,
        )
        vision = await asyncio.create_subprocess_exec(
            str(VISION),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=6_000_000,
        )
        capture.stdin.write(
            (
                json.dumps(
                    {"source": "miloco", "camera": SELECTED["id"], "motor": bool(camera_task),
                     "audio": True, "identity": False}
                )
                + "\n"
            ).encode()
        )
        await capture.stdin.drain()
        queue = asyncio.Queue(maxsize=1)
        latest_item = None
        last_selection = None
        scene_data = {"objects": [], "observed_at": 0}

        async def observe_scene():
            nonlocal scene_data
            detector = await asyncio.to_thread(SceneDetector, MODELS / "yolov8n.onnx")
            processed_at = None
            while my_generation == generation:
                item = latest_item
                if (item and item.get("type") == "frame" and item['received_at'] != processed_at
                        and 0 <= time.time()-item['received_at'] <= 1.2):
                    processed_at = item['received_at']
                    result = await asyncio.to_thread(detector.detect, base64.b64decode(item["jpeg"]))
                    result["observed_at"] = item["received_at"]
                    scene_data = result
                await asyncio.sleep(0.7)

        last_video_lease=0.0
        async def receive():
            nonlocal latest_item,last_video_lease
            while raw := await capture.stdout.readline():
                item = json.loads(raw)
                if item.get("type") == "diagnostic":
                    state["capture_diagnostic"] = dict(
                        stage=item.get("stage"),
                        at=time.time(),
                        categories=item.get("categories"),
                        codes=item.get("codes"),
                    )
                    state.setdefault("capture_stages", []).append(state["capture_diagnostic"])
                    state["capture_stages"] = state["capture_stages"][-30:]
                    continue
                if item.get("type") == "audio":
                    home.audio_allowed = home.audio_allowed and fleet.voice_current(SELECTED["id"], time.monotonic())
                    await home.audio(item)
                    continue
                if item.get("type") == "motor_result":
                    future = motor_waiters.get(item.get("request_id"))
                    if future and not future.done():
                        future.set_result(item)
                    continue
                if item.get('type')=='video':
                    if state.get('status')!='shutter_closed':wall.offer(SELECTED['id'],wall_epoch,item)
                    continue
                latest_item = item
                if item.get("type") == "frame":
                    if time.monotonic()-last_video_lease>.7:
                        last_video_lease=time.monotonic()
                        capture.stdin.write((json.dumps(dict(action='video_window',enabled=wall.video[SELECTED['id']].subscribers>0))+'\n').encode())
                    captured_times.append(time.monotonic())
                    if state.get('status') != 'shutter_closed':wall.offer(SELECTED['id'], wall_epoch, item)
                offer_frame(queue, item)
            offer_frame(queue, {"type": "error", "reason": "source_closed"})

        receiver = asyncio.create_task(receive())
        scene_task = asyncio.create_task(observe_scene())
        try:
            while my_generation == generation:
                if camera_task and camera_task.done():
                    if state['status']=='shutter_closed':return
                    raise RuntimeError('camera_owner_unavailable')
                stage = "waiting_frame"
                item = await asyncio.wait_for(queue.get(), 85)
                if item["type"] != "frame":
                    state.update(
                        status="unavailable",
                        reason=item.get("reason", "source_unavailable"),
                        error_class=item.get("error_class"),
                        error_stage="capture",
                        frame=None,
                        people=[],
                        hands=[],
                        room=None,
                        game={
                            "mode": games.mode,
                            "status": "paused",
                            "reason": "实时画面已过期",
                            "players": [],
                        },
                    )
                    await notify()
                    break
                if not 0 <= time.time() - item['received_at'] <= 1.5:
                    safety.clear(SELECTED['id'])
                    fleet.clear(SELECTED['id'])
                    home.selected = False
                    home.epoch += 1
                    identities.discard()
                    continue
                frame_motion_generation = motion_generation
                phase_at = time.monotonic()
                timings = dict(queue_age_ms=round((time.time()-item['received_at'])*1000,1))
                # Pixel data remains exclusively in local process pipes and memory.
                current_scene = (
                    scene_data if time.time() - scene_data["observed_at"] < 1.2 else {"objects": []}
                )
                if room is not None and room.reg_info.get("status") in ("moving", "settling"):
                    current_scene = {"objects": []}
                rois = [
                    o["box"]
                    for o in current_scene["objects"]
                    if o["class"] == "person" and o["confidence"] >= 0.5
                ]
                vision.stdin.write(
                    (
                        json.dumps(
                            {
                                "jpeg": item["jpeg"],
                                "face_jpeg": item.get("face_jpeg"),
                                "person_rois": rois,
                                "hand_interval_ms": 140 if games.mode not in ("off", "home") else 80,
                                "attention_enabled": True,
                            }
                        )
                        + "\n"
                    ).encode()
                )
                await vision.stdin.drain()
                stage = "vision_request"
                detection = json.loads(await asyncio.wait_for(vision.stdout.readline(), 12))
                timings['vision_ms'] = round((time.monotonic()-phase_at)*1000,1)
                phase_at = time.monotonic()
                if detection.get("error"):
                    raise RuntimeError("vision_request_failed")
                if my_generation != generation:
                    break
                if state.get('status') == 'shutter_closed':return
                if frame_motion_generation != motion_generation or not 0 <= time.time()-item['received_at'] <= 1.5:
                    state['pipeline_timing'] = dict(timings, dropped_stage='after_vision')
                    safety.clear(SELECTED['id'])
                    fleet.clear(SELECTED['id']);home.selected=False;home.epoch+=1;identities.discard()
                    continue
                now = time.monotonic()
                picture = base64.b64decode(item["jpeg"])
                if room is None:
                    config = (
                        json.loads(calibration_path.read_text())
                        if calibration_path.exists()
                        else copy.deepcopy(DEFAULT)
                    )
                    if SELECTED["id"] != configuration()["default_camera"] and not calibration_path.exists():
                        config.update(
                            mode="unconfigured",
                            furniture=[],
                            seats=copy.deepcopy(DEFAULT["seats"]),
                            note="该镜头尚未标定；点位不可用",
                        )
                    reference = reference_path.read_bytes() if reference_path.exists() else picture
                    room = Room(config, reference)
                    if not reference_path.exists():
                        reference_path.write_bytes(picture)
                        calibration_path.write_text(json.dumps(config, ensure_ascii=False, indent=2))
                hands = detection.get("hands", [])
                async with room_lock:
                    await asyncio.to_thread(
                        room.register, picture, [{"box": b} for b in detection.get("boxes", [])], now
                    )
                    people = tracks.update(detection, now, transform=room.registration,
                        camera_moving=now < room.command_until or room.reg_info.get('status') in {'moving','settling'})
                    layout = await asyncio.to_thread(
                        room.update,
                        picture,
                        people,
                        hands,
                        now,
                        item["width"],
                        item["height"],
                        current_scene["objects"],
                        True,
                    )
                if room.config.get("mode") == "unconfigured":
                    layout.update(
                        seats=[],
                        pointing=[],
                        furniture=[],
                        observed_furniture=[],
                        position_status="unconfigured",
                    )
                    for p in people:
                        p["position"] = None
                if layout["registration"]["status"] in ("moving", "settling"):
                    for p in people:
                        p["motion"] = "camera_moving"
                global last_preview, last_preview_at
                if item.get("preview"):
                    last_preview = base64.b64decode(item["preview"])
                    last_preview_at = item["preview_at"]
                timings['layout_ms'] = round((time.monotonic()-phase_at)*1000,1)
                phase_at = time.monotonic()
                identity_rows = await analyze(
                    identities, base64.b64decode(item.get("face_jpeg") or item["jpeg"]), people,
                    detection.get("faces", []), moving=now < room.command_until or layout["registration"]["status"] in ("moving", "settling"),
                    sample=bool(item.get('face_jpeg')) or max(item.get('native_size') or [0])<=1920,
                    captured_at=time.monotonic()-(time.time()-item['received_at']))
                timings['identity_ms'] = round((time.monotonic()-phase_at)*1000,1)
                phase_at = time.monotonic()
                scene_visible = await asyncio.to_thread(visible_scene, picture)
                timings['visibility_ms'] = round((time.monotonic()-phase_at)*1000,1)
                state['pipeline_timing'] = dict(timings)
                if state.get('status')=='shutter_closed':return
                if frame_motion_generation != motion_generation or not 0 <= time.time() - item['received_at'] <= 1.5:
                    state['pipeline_timing']['dropped_stage'] = 'after_identity'
                    safety.clear(SELECTED['id'])
                    fleet.clear(SELECTED['id'])
                    home.selected = False
                    home.epoch += 1
                    identities.discard()
                    continue
                by_track = {r["track"]: r for r in identity_rows}
                for person in people:
                    person["identity"] = by_track.get(str(person["id"]), dict(state="unknown", person_id=None))
                state["identity"] = dict(reason=identities.reason, tracks=identity_rows)
                await safety.observe(SELECTED['id'], identities.session, people, now,
                    time.monotonic() - (time.time() - item['received_at']),
                    stable=scene_visible and layout['registration'].get('status') == 'locked'
                        and now >= room.command_until,
                    jpeg=picture, aspect=item['width'] / max(1, item['height']))
                wall.observe(SELECTED['id'],identities.session,people,item['received_at'],
                    hands=hands,
                    moving=layout['registration'].get('status') in {'moving','settling','lost','stale'})
                game = games.update(people, layout["registration"], now, hands)
                extension_events = registry.observe(
                    camera=SELECTED["id"], session=identities.session, people=people, hands=hands, game=game, now=now
                )
                for event in extension_events:
                    home.spawn(
                        bindings.dispatch(
                            event,
                            camera=SELECTED["id"],
                            session=identities.session,
                            bound_track=games.preferred_track,
                            now=now,
                            current=lambda e=event, g=my_generation: (
                                generation == g
                                and interaction_enabled
                                and games.preferred_track == e.track
                                and 0 <= time.monotonic() - e.observed_mono <= 1.5
                            ),
                        )
                    )
                state["extension_events"] = [e.model_dump() for e in extension_events]
                phase_at = time.monotonic()
                if interaction_enabled:
                    await home.enable()
                elif not interaction_enabled:
                    home.armed = False
                if state.get('status')=='shutter_closed':return
                last_selection = games.selected_mode
                gesture_active = interaction_enabled and games.mode in {'off', 'home'}
                interaction_events=fleet.update(SELECTED['id'],identities.session,people,hands,
                    layout['registration'],gesture_active,now)
                voice_now = time.monotonic()
                home.audio_allowed=fleet.voice(SELECTED['id'],voice_now,home.attention.candidate() is not None,
                    people=len(people), session=identities.session,
                    speaking=bool(home.speaking and home.last_audio is not None and 0 <= voice_now-home.last_audio <= .6),
                    enabled=interaction_enabled and home.armed and 0 <= voice_now-now <= 1.5
                        and not (PRIVATE/'voice-input-paused').exists())
                await home.observe(
                    people,
                    detection.get("faces", []),
                    layout["registration"],
                    now,
                    capture,
                    interaction_enabled and home.armed,
                    fleet.cameras.get(SELECTED['id'], {}).get('owner'),
                    gestures_enabled=False,
                )
                if state.get('status')=='shutter_closed':return
                await dispatch_interactions(interaction_events)
                timings['interaction_ms'] = round((time.monotonic()-phase_at)*1000,1)
                state['pipeline_timing'] = dict(timings)
                processed_times.append(time.monotonic())
                last_jpeg = picture
                last_frame = time.monotonic()-(time.time()-item['received_at'])
                state.update(
                    status="live",
                    scene_visible=scene_visible,
                    reason=None,
                    frame=item["jpeg"],
                    width=item["width"],
                    height=item["height"],
                    error_class=None,
                    error_stage=None,
                    received_at=item["received_at"],
                    processed_frames=state["processed_frames"] + 1,
                    people=people,
                    hands=hands,
                    room=layout,
                    game=game,
                    scene=current_scene,
                    inference_ms=round(detection.get("inference_ms", 0), 1),
                    body_ms=round(detection.get("body_ms", 0), 1),
                    hand_ms=round(detection.get("hand_ms", 0), 1),
                    capture_fps=rate(captured_times),
                    processed_fps=rate(processed_times),
                    capture_encode_ms=item.get("capture_encode_ms"),
                    native_size=item.get("native_size"),
                    stream_quality=item.get("stream_quality"),
                    host_decode_ms=item.get("host_decode_ms"),
                    raw_fps=item.get("raw_fps"),
                    decoded_fps=item.get("decoded_fps"),
                    hardware_decoding=item.get("hardware_decoding"),
                    pipeline_ms=round((time.time() - item["received_at"]) * 1000, 1),
                )
                latest = now
                await notify()
        finally:
            receiver.cancel()
            scene_task.cancel()
            await asyncio.gather(receiver, scene_task, return_exceptions=True)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        if my_generation == generation:
            state.update(
                status="unavailable",
                reason="source_or_detector_unavailable",
                error_class=type(error).__name__,
                error_stage=stage,
                frame=None,
                people=[],
                hands=[],
                room=None,
                game={"mode": games.mode, "status": "paused", "reason": "实时画面已过期", "players": []},
            )
            await notify()
    finally:
        wall.clear(SELECTED['id'], wall_epoch)
        safety.clear(SELECTED['id'])
        identities.close()
        if camera_task:
            camera_task.cancel()
            await asyncio.gather(camera_task, return_exceptions=True)
        await reap(capture)
        await reap(vision)
        capture = vision = None


async def supervised_source(my_generation):
    retry = 0
    while my_generation == generation:
        await run_source(my_generation)
        if my_generation != generation:
            return
        retry += 1
        state["reconnect_attempt"] = retry
        await asyncio.sleep(15 if state['status'] == 'shutter_closed' else min(30, 2 ** min(retry, 5)))


async def start():
    global task, generation, last_frame
    if task and not task.done():
        return
    generation += 1
    last_frame = 0
    task = asyncio.create_task(supervised_source(generation))
    for selected in configuration()["cameras"]:
        if selected["id"] != SELECTED["id"]:
            await previews.start(selected["id"])


async def stop():
    global task, generation, last_frame
    home.selected = False
    home.armed = False
    home.vad = []
    home.pending_audio = None
    home.gesture.reset()
    generation += 1
    if task:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        task = None
    await previews.close()
    for camera_id in list(fleet.cameras):clear_camera(camera_id)
    for service in secondary_homes.values():
        service.armed=False
        await service.close()
    secondary_homes.clear()
    last_frame = 0
    games.reset("off")
    state.update(
        status="stopped",
        reason=None,
        frame=None,
        people=[],
        hands=[],
        room=None,
        game={"mode": games.mode, "status": "paused", "reason": "实时画面已过期", "players": []},
    )
    await notify()


@asynccontextmanager
async def lifespan(app):
    if os.environ.get("FRIDAY_START_IDLE") != "1":
        await start()
    else:
        state["camera_control"] = {"owner": "none", "phase": "idle", "reason": "识别尚未启动"}
    yield
    await stop()
    await previews.close()
    await home.close()
    await safety.close()
    identity_store.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.get('/api/safety/status')
async def safety_status():
    return safety.status()


def identity_authorized(request):
    if request.client.host not in {"127.0.0.1", "::1"} or not secrets.compare_digest(
        request.headers.get("x-friday-worker", ""), (PRIVATE / "control-key").read_text().strip()
    ):
        raise HTTPException(403)


@app.get("/api/internal/identity")
async def identity_status(request: Request):
    identity_authorized(request)
    primary=dict(camera=SELECTED["id"], session=identities.session, state=view()['status'],
                reason=identities.reason, diagnostics=identities.diagnostics(), tracks=identities.public() if view()['status']=='live' else [])
    sessions=[dict(primary)]
    for camera_id,item in previews.sessions.items():
        recognizer=item.get('recognizer')
        report=previews.status(camera_id)
        sessions.append(dict(recognizer.identity_status(),state=report['state']) if recognizer and report['state']=='live'
            else dict(camera=camera_id,state=report['state'],tracks=[]))
    return dict(primary,sessions=sessions)


@app.post("/api/internal/identity/register")
async def identity_register(request: Request):
    identity_authorized(request)
    body = await request.json()
    if set(body) != {"camera", "session", "track", "person"}:
        raise HTTPException(409, "identity_session_changed")
    pipeline=identities if body['camera']==SELECTED['id'] and view()['status']=='live' else None
    if pipeline is None:
        entry=previews.sessions.get(body['camera'],{})
        recognizer=entry.get('recognizer')
        if recognizer and previews.status(body['camera'])['state']=='live':pipeline=recognizer.identities
    if pipeline is None or body['session']!=pipeline.session:raise HTTPException(409,'identity_session_changed')
    try:
        return await asyncio.to_thread(pipeline.enroll, body["track"], body["person"])
    except (TypeError, ValueError) as error:
        raise HTTPException(409, str(error)) from None


@app.middleware("http")
async def privacy(request, call_next):
    audio_access = (
        request.url.path.startswith("/audio/")
        and request.client.host in configuration().get("speaker_hosts", [])
        and secrets.compare_digest(request.query_params.get("k", ""), KEY)
    )
    if (
        request.url.path not in {"/health", "/", "/games"}
        and not request.url.path.startswith("/assets/")
        and not audio_access
        and not authorized(request.client, request.query_params.get("k"))
    ):
        return JSONResponse({"error": "viewer_access_required"}, status_code=403)
    limit = (
        8 * 1024 * 1024
        if request.url.path == "/api/record/chunk"
        else 3_000_000
        if request.url.path == "/api/calibration"
        else 16384
    )
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > limit:
                return JSONResponse({"code": "request_budget_exceeded"}, status_code=413)
        request._body = bytes(body)
        origin = request.headers.get("origin")
        if origin and origin != "http://" + request.headers.get("host", ""):
            return JSONResponse({"code": "unauthorized"}, status_code=403)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors "+configuration()['main_origin']
    )
    return response


@app.post('/api/interaction')
async def interaction_policy(request: Request):
    global interaction_enabled
    body = await request.json()
    if set(body) != {'enabled'} or type(body['enabled']) is not bool:
        raise HTTPException(422)
    interaction_enabled = body['enabled']
    if not interaction_enabled:
        for camera_id in list(fleet.cameras):
            clear_camera(camera_id)
        for service in [home, *secondary_homes.values()]:
            service.armed = service.selected = False
            service.epoch += 1
            service.vad = []
            service.pending_audio = None
    return dict(enabled=interaction_enabled)


@app.get("/assets/{name}")
async def asset(name: str):
    if name not in {"index.js", "index.css", "games.js", "games.css"}:
        raise HTTPException(404)
    return FileResponse(HERE / "static" / name)


@app.get("/audio/{token}")
async def speech_audio(token: str):
    import re

    if not re.fullmatch(r"[a-f0-9]{32}", token):
        raise HTTPException(404)
    path = DATA / "speech" / ("reply-" + token + ".m4a")
    if not path.is_file() or time.time() - path.stat().st_mtime > 180:
        raise HTTPException(404)
    return FileResponse(path, media_type="audio/mp4")


@app.post("/api/internal/cover")
async def internal_cover(request: Request):
    if request.client.host not in {"127.0.0.1", "::1"} or not secrets.compare_digest(
        request.headers.get("x-friday-worker", ""), (PRIVATE / "control-key").read_text().strip()
    ):
        raise HTTPException(403)
    from datetime import UTC, datetime

    from homeplane.action_contracts import HomeDeviceAuthority

    policy = HomeDeviceAuthority.model_validate_json((PRIVATE / "action-authority.json").read_bytes())
    if not policy.enabled or policy.expires_at is not None and policy.expires_at <= datetime.now(UTC):
        raise HTTPException(403)
    data = await request.json()
    action = data.get("action")
    event = data.get("event_id")
    if action not in ("open", "close") or not isinstance(event, str) or not 1 <= len(event) <= 160:
        raise HTTPException(400)
    # Durable event claim prevents voice retries after restart from sending twice.
    import hashlib

    ledger = PRIVATE / "cover-ledger.json"
    entries = json.loads(ledger.read_text()) if ledger.exists() else {}
    key = hashlib.sha256(event.encode()).hexdigest()
    if key in entries:
        return entries[key]
    if home.curtain.api is None:
        await home.curtain.connect()
    entries[key] = {"status": "unknown_outcome", "acknowledged": False, "reached_target": False}
    from homeplane.embodied.ptz_control import atomic

    atomic(ledger, dict(list(entries.items())[-128:]))
    result = await home.curtain.act(action, event)
    entries[key] = {k: result.get(k) for k in ("status", "acknowledged", "reached_target")}
    atomic(ledger, dict(list(entries.items())[-128:]))
    return result


@app.post("/api/voice")
async def voice_control(request: Request):
    if request.client.host not in {"127.0.0.1", "::1"}:
        raise HTTPException(403)
    data = await request.json()
    if set(data) not in ({'quiet'},{'input'}) or type(next(iter(data.values()),None)) is not bool:
        raise HTTPException(400)
    is_input='input' in data
    marker = PRIVATE / ('voice-input-paused' if is_input else 'voice-paused')
    paused=not data['input'] if is_input else data['quiet']
    if paused:
        marker.touch(mode=0o600)
    else:
        marker.unlink(missing_ok=True)
    if is_input:
        armed=home.armed
        for service in [*secondary_homes.values(),home]:await service.close()
        if armed:await home.enable()
    return {'quiet':(PRIVATE/'voice-paused').exists(),'voice_input':not (PRIVATE/'voice-input-paused').exists()}


@app.post("/api/previews/{camera_id}")
async def start_preview(camera_id: str, request: Request):
    if request.client.host not in {"127.0.0.1", "::1"}:
        raise HTTPException(403)
    if camera_id == SELECTED["id"]:
        return {"state": view()["status"], "camera": camera_id, "primary": True}
    return await previews.start(camera_id)


@app.get("/api/previews/{camera_id}")
async def preview_status(camera_id: str, request: Request):
    if request.client.host not in {"127.0.0.1", "::1"}:
        raise HTTPException(403)
    if camera_id == SELECTED["id"]:
        return {
            "state": view()["status"],
            "camera": camera_id,
            "frames": state["processed_frames"],
            "primary": True,
        }
    return previews.status(camera_id)


@app.get("/api/previews/{camera_id}/frame")
async def preview_frame(camera_id: str, request: Request):
    if request.client.host not in {"127.0.0.1", "::1"}:
        raise HTTPException(403)
    frame = (
        last_preview
        if camera_id == SELECTED["id"] and view()["status"] == "live"
        else previews.frame(camera_id)
    )
    if not frame:
        raise HTTPException(409, detail="camera_frame_unavailable")
    return Response(frame, media_type="image/jpeg")


@app.get('/api/wall')
async def camera_wall_status():
    primary = view()
    states = {c['id']: previews.status(c['id']) for c in configuration()['cameras'] if c['id'] != SELECTED['id']}
    states[SELECTED['id']] = dict(state=primary['status'],
        recognition='running' if primary['status']=='live' else 'waiting',
        people=len(primary.get('people', [])), hands=len(primary.get('hands', [])))
    result=wall.view(states)
    names=identity_store.labels()
    for camera in result['cameras']:
        observation=camera.get('overlay')
        if observation:
            camera['overlay']=dict(observation,people=[dict(p,**({'name':names[p['person_id']]}
                if p.get('person_id') in names else {})) for p in observation['people']])
    return JSONResponse(result, headers={'Cache-Control':'no-store'})


@app.get('/api/wall/{camera_id}/frame')
async def camera_wall_frame(camera_id: str):
    if camera_id not in wall.epochs:raise HTTPException(404)
    value = wall.frame(camera_id)
    if value is None:raise HTTPException(409, detail='camera_frame_unavailable')
    return Response(value[0], media_type='image/jpeg', headers={'Cache-Control':'no-store',
        'X-Friday-Frame-Age-Ms':str(max(0, round((time.time()-value[1])*1000))),
        'X-Friday-Frame-At':str(value[1])})


@app.get('/api/wall/{camera_id}/video')
async def camera_wall_video(camera_id: str):
    relay=wall.video.get(camera_id)
    if relay is None:raise HTTPException(404)
    if relay.subscribers>=2:raise HTTPException(429)
    return StreamingResponse(relay.stream(),media_type='application/octet-stream',
        headers={'Cache-Control':'no-store','X-Friday-Video':'annex-b-v1'})


@app.get('/api/wall/{camera_id}/still')
async def camera_wall_still(camera_id: str):
    if camera_id not in wall.epochs:raise HTTPException(404)
    value=wall.still(camera_id)
    if value is None:raise HTTPException(409,detail='camera_frame_unavailable')
    return Response(value[0],media_type='image/jpeg',headers={'Cache-Control':'no-store',
        'X-Friday-Frame-Unix-Ms':str(int(value[1]*1000))})


@app.post('/api/previews/{camera_id}/control')
async def preview_control(camera_id: str, request: Request):
    if request.client.host not in {'127.0.0.1','::1'}:
        raise HTTPException(403)
    data = await request.json()
    direction, mode = data.get('direction'), data.get('mode')
    if bool(direction)==bool(mode):raise HTTPException(400)
    if camera_id == SELECTED['id']:
        if direction:
            if not camera_planner:raise HTTPException(409, detail='camera_not_owned')
            camera_planner.mode = 'hold'
            return await manual_motor(request)
        return await camera_control(request)
    try:
        return await previews.control(camera_id,direction=direction,mode=mode)
    except ValueError:raise HTTPException(400) from None
    except (RuntimeError,OSError,TimeoutError):
        raise HTTPException(409,detail='motor_not_ready') from None


@app.get("/")
async def index():
    return HTMLResponse((HERE / "static/index.html").read_text())


@app.get("/games")
async def game_page():
    return HTMLResponse((HERE / "static/games.html").read_text())


@app.post("/api/game")
async def game_control(request: Request):
    origin = request.headers.get("origin")
    if origin and origin != "http://" + request.headers.get("host", ""):
        raise HTTPException(403)
    data = await request.json()
    try:
        if data.get("action") == "select":
            games.select(data.get("mode", "quest"))
        else:
            games.reset(data.get("mode", "off"))
        if data.get("action") == "start" and games.mode != "off":
            await start()
        if games.selected_mode == "home" and data.get("mode") != "off":
            await home.enable()
            await start()
    except ValueError:
        raise HTTPException(400)
    return games.public


@app.post("/api/home/curtain")
async def curtain_control(request: Request):
    origin = request.headers.get("origin")
    if origin and origin != "http://" + request.headers.get("host", ""):
        raise HTTPException(403)
    if not interaction_enabled or not home.enabled or not home.armed:
        raise HTTPException(409, detail="home_mode_required")
    data = await request.json()
    action = data.get("action")
    if action not in ("open", "close"):
        raise HTTPException(400)
    home.spawn(home.command(action, "manual:" + secrets.token_hex(10), "explicit_button"))
    return {"status": "accepted", "action": action}


@app.post("/api/record")
async def record_control(request: Request):
    origin = request.headers.get("origin")
    if origin and origin != "http://" + request.headers.get("host", ""):
        raise HTTPException(403)
    data = await request.json()
    action = data.get("action")
    async with record_lock:
        try:
            if action == "arm":
                return recorder.arm(data.get("enabled") is True)
            if request.client.host not in {"127.0.0.1", "::1"}:
                raise HTTPException(403)
            if action == "start":
                return recorder.start(data.get("mime"))
            if action == "finish":
                return recorder.finish(data.get("id"))
            if action == "error":
                if recorder.state["status"] not in ("recording", "stopping"):
                    recorder.state.update(
                        status="error", armed=False, reason=str(data.get("reason", ""))[:120]
                    )
                    recorder.persist()
                return recorder.public()
            raise ValueError("invalid_recording_action")
        except ValueError as e:
            raise HTTPException(409, detail=str(e))


@app.post("/api/record/chunk")
async def record_chunk(request: Request):
    if request.client.host not in {"127.0.0.1", "::1"}:
        raise HTTPException(403)
    origin = request.headers.get("origin")
    if origin and origin != "http://" + request.headers.get("host", ""):
        raise HTTPException(403)
    chunks = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > 8 * 1024 * 1024:
            raise HTTPException(413)
        chunks.append(chunk)
    async with record_lock:
        try:
            return recorder.append(request.query_params.get("id"), b"".join(chunks))
        except ValueError as e:
            raise HTTPException(409, detail=str(e))


@app.get("/api/record/file")
async def recording_file(request: Request):
    if (
        request.query_params.get("id") != recorder.state.get("id")
        or recorder.state["status"] != "saved"
        or recorder.path is None
    ):
        raise HTTPException(404)
    return FileResponse(recorder.path, media_type=recorder.mime, filename=recorder.path.name)


@app.get("/health")
async def health():
    return {"ready": True, "video_live": view()["status"] == "live"}


@app.get("/api/status")
async def status():
    result = view()
    result.pop("frame", None)
    return result


@app.get("/api/frame")
async def frame():
    return view()


@app.get("/api/live")
async def live(request: Request):
    async def stream():
        previous = -1
        while not await request.is_disconnected():
            if previous == state["processed_frames"]:
                async with changed:
                    try:
                        await asyncio.wait_for(changed.wait(), 1)
                    except asyncio.TimeoutError:
                        pass
            meta = view()
            meta.pop("frame", None)
            meta["sent_at"] = time.time()
            previous = meta["processed_frames"]
            jpeg = last_jpeg if meta["status"] == "live" else b""
            encoded = json.dumps(meta, separators=(",", ":"), ensure_ascii=False).encode()
            yield struct.pack(">II", len(encoded), len(jpeg)) + encoded + jpeg

    return StreamingResponse(
        stream(), media_type="application/octet-stream", headers={"X-Friday-Protocol": "1"}
    )


@app.get("/api/calibration")
async def calibration():
    async with room_lock:
        if room is None:
            return DEFAULT
        config = copy.deepcopy(room.config)
        if room.registration is not None:
            import numpy as np

            inverse = np.linalg.inv(room.registration)
            for item in config["anchors"] + config["seats"]:
                point = project(inverse, item["image"])
                if point:
                    item["image"] = point
        return config


@app.post("/api/calibration")
async def save_calibration(request: Request):
    global room
    if request.client.host not in {"127.0.0.1", "::1"}:
        raise HTTPException(403)
    origin = request.headers.get("origin")
    if origin and origin != "http://" + request.headers.get("host", ""):
        raise HTTPException(403)
    if int(request.headers.get("content-length", "0")) > 3_000_000:
        raise HTTPException(413)
    data = await request.json()
    try:
        config = data["config"]
        validate(config)
        reference = base64.b64decode(data["reference_jpeg"], validate=True)
        if len(reference) > 2_000_000:
            raise ValueError("reference_too_large")
        manual = config.get("mode") == "relative_calibrated"
        config.update(
            mode="relative_calibrated" if manual else "approximate",
            metric=False,
            note="相对坐标 · 手动标定 · 无米制尺寸" if manual else "按米家户型粗标 · 相对坐标 · 座位待校准",
        )
        replacement = Room(config, reference)
    except Exception as error:
        raise HTTPException(400, detail=str(error)[:80])
    calibration_path.write_text(json.dumps(config, ensure_ascii=False, indent=2))
    reference_path.write_bytes(reference)
    async with room_lock:
        room = replacement
    if camera_planner:
        camera_planner.views.clear()
    return {"saved": True, "metric": False}


@app.post("/api/viewer")
async def viewer(request: Request):
    if request.client.host not in configuration().get("tv_viewer_hosts", []):
        raise HTTPException(403)
    data = await request.json()
    state["tv_viewer"] = {
        k: data.get(k)
        for k in (
            "version",
            "cameras",
            "frames",
            "fps",
            "decode_ms",
            "server_frame_age_ms",
            "mode",
            "event",
            "detail",
            "progress",
        )
    }
    state["tv_viewer"]["reported_at"] = time.time()
    return {"ok": True}


@app.post("/api/camera")
async def camera_control(request: Request):
    if request.client.host not in {"127.0.0.1", "::1"}:
        raise HTTPException(403)
    origin = request.headers.get("origin")
    if origin and origin != "http://" + request.headers.get("host", ""):
        raise HTTPException(403)
    data = await request.json()
    mode = data.get("mode")
    if mode not in ("auto", "hold", "survey"):
        raise HTTPException(400)
    if not camera_planner:
        raise HTTPException(409, detail="camera_not_owned")
    camera_planner.mode = mode
    if mode == "survey":
        camera_planner.visited.clear()
        camera_planner.last_person = time.monotonic() - 8
        camera_planner.last_move = time.monotonic() - 12
    return {"mode": mode}


@app.post("/api/motor")
async def manual_motor(request: Request):
    if request.client.host not in {"127.0.0.1", "::1"}:
        raise HTTPException(403)
    origin = request.headers.get("origin")
    if origin and origin != "http://" + request.headers.get("host", ""):
        raise HTTPException(403)
    data = await request.json()
    try:
        return await motor_step(data.get("direction"))
    except ValueError:
        raise HTTPException(400)
    except RuntimeError:
        raise HTTPException(409, detail="motor_not_ready")


@app.post("/api/{action}")
async def control(action: str, request: Request):
    origin = request.headers.get("origin")
    if origin and origin != "http://" + request.headers.get("host", ""):
        raise HTTPException(403)
    if action == "start":
        await start()
    elif action == "stop":
        await stop()
    elif action == "seats_reset":
        if room:
            room.players.clear()
            room.pending.clear()
            room.next_seat = 1
            room.holds.clear()
    else:
        raise HTTPException(404)
    return {"status": view()["status"]}


@app.websocket("/ws")
async def websocket(ws: WebSocket):
    if not authorized(ws.client, ws.query_params.get("k")):
        await ws.close(code=1008)
        return
    origin = ws.headers.get("origin")
    if origin and origin != "http://" + ws.headers.get("host", ""):
        await ws.close(code=1008)
        return
    await ws.accept()
    try:
        previous = -1
        preview_sent = 0.0
        while True:
            if previous == state["processed_frames"]:
                async with changed:
                    try:
                        await asyncio.wait_for(changed.wait(), 1)
                    except asyncio.TimeoutError:
                        pass
            meta = view()
            meta.pop("frame", None)
            meta["sent_at"] = time.time()
            previous = meta["processed_frames"]
            jpeg = last_jpeg if meta["status"] == "live" else b""
            if ws.query_params.get("view") == "game":
                jpeg = (
                    last_preview
                    if meta["status"] == "live"
                    and last_preview_at != preview_sent
                    and time.time() - last_preview_at < 1.5
                    else b""
                )
                if jpeg:
                    preview_sent = last_preview_at
                meta["preview_at"] = last_preview_at
            encoded = json.dumps(meta, separators=(",", ":"), ensure_ascii=False).encode()
            await asyncio.wait_for(
                ws.send_bytes(struct.pack(">II", len(encoded), len(jpeg)) + encoded + jpeg), 2
            )
    except Exception:
        pass


async def serve():
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="0.0.0.0",
            port=int(configuration()["viewer_port"]),
            log_level="warning",
            access_log=False,
            proxy_headers=False,
            timeout_graceful_shutdown=2,
        )
    )
    parent = os.getppid()

    async def owner_watch():
        while os.getppid() == parent:
            await asyncio.sleep(1)
        server.should_exit = True

    watch = asyncio.create_task(owner_watch())
    try:
        await server.serve()
    finally:
        watch.cancel()
        await asyncio.gather(watch, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(serve())
