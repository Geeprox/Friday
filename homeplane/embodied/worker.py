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
from homeplane.embodied.room_session import RoomSession, close_rooms
from homeplane.embodied.room_protocol import unavailable_layout
from homeplane.embodied.scene_process import SceneProcess
from homeplane.embodied.calibration_save import CalibrationSaver
from homeplane.embodied.calibration_frames import CalibrationFrames
from homeplane.embodied.static_page import static_asset, static_page
from homeplane.embodied.camera_director import CameraDirector
from homeplane.embodied.party_camera import PartyCamera
from homeplane.embodied.camera_intent import CAMERA_MODES, PET_MODES, CameraIntentStore
from homeplane.embodied.camera_follow_picker import FollowCommand
from homeplane.embodied.frame_sampling import CAPTURE_RECEIVE_TIMEOUT, offer_frame
from homeplane.embodied.camera_wall import CameraWall
from homeplane.embodied.stable_tracks import StableTracks as Tracks
from homeplane.embodied.pet_tracks import PetTracks
from homeplane.embodied.camera_audio import AudioIngress, deliver_audio
from homeplane.embodied.vision_pipe import request_vision
from homeplane.people.live import IdentityStore, analyze, close_identity
from homeplane.people.identity_session import IdentitySession

os.umask(0o077)
KEY_PATH = PRIVATE / "viewer-key"
KEY = KEY_PATH.read_text().strip()
ALLOWED_IPS = set(configuration()["viewer_hosts"])
SELECTED = dict(camera())
wall = CameraWall(configuration()['cameras'])
primary_wall_epoch = 0
primary_retired_scope = None
if os.environ.get("FRIDAY_BACKGROUND") == "1":
    SELECTED["takeover"] = True
interaction_enabled = os.environ.get("FRIDAY_INTERACTION_ENABLED", "1") == "1"
CAMERA_STORAGE = camera_storage()
camera_intents = CameraIntentStore(CAMERA_STORAGE, SELECTED)
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
pet_tracks = PetTracks()
latest = None
last_frame = 0
last_jpeg = b""
last_preview = b""
last_preview_at = 0.0
changed = asyncio.Condition()
room = None
calibration_frames = CalibrationFrames()
processed_times = collections.deque(maxlen=120)
captured_times = collections.deque(maxlen=120)
room_lock = asyncio.Lock()
camera_lease = None
camera_planner = None
motor_waiters = {}
motor_sequence = 0
motion_generation = 0
motor_last_sent = -100.0
games = MotionGames(require_presentation=True)
party_camera = PartyCamera()
recorder = PageRecording(DATA / "recordings")
record_lock = asyncio.Lock()
home = HomeService(SELECTED['id'])
from homeplane.embodied.interaction import InteractionEngine
from homeplane.embodied.interaction_fleet import InteractionFleet
from homeplane.embodied.practice import RecognitionPractice
practice = RecognitionPractice()
interaction = InteractionEngine(SELECTED["id"], SESSION)
fleet = InteractionFleet()
secondary_homes = {}
identity_store = IdentityStore(PRIVATE / "identities.sqlite")
from homeplane.embodied.safety import SafetyRuntime, SafetyStore
safety = SafetyRuntime(SafetyStore(PRIVATE / "safety.sqlite"), labels=identity_store.labels)
identities = IdentitySession(identity_store, SELECTED["id"], SESSION, MODELS)


def interaction_source_current(event):
    if event['camera'] == SELECTED['id']:
        return (state.get('status') in {'connecting', 'live'}
                and event['session'] == identities.session)
    item = previews.sessions.get(event['camera'], {})
    recognizer = item.get('recognizer')
    return bool(recognizer and recognizer.session == event['session']
                and previews.source_current(event['camera'], item.get('process')))


async def dispatch_interactions(events):
    for event in events:
        game_window = event.get('_game_window')
        if event['camera'] == SELECTED['id'] and game_window is None:
            game_window = (games.control_context, games.control_revision)
        party_window=event.get('_party_window',party_camera.window(time.monotonic())[:2])if event['camera']==SELECTED['id']else None
        async def dispatch(e=event, expected_game=game_window, expected_party=party_window):
            if e['camera'] == SELECTED['id'] and (
                    games.mode not in {'off', 'home'}
                    or expected_game != (games.control_context, games.control_revision)):
                return
            if e['camera']==SELECTED['id']:
                party=party_camera.window(time.monotonic())
                if party[2]or party[:2]!=expected_party:return
            service = home if e['camera'] == SELECTED['id'] else secondary_homes.get(e['camera'])
            if not (interaction_enabled and service and service.armed and interaction_source_current(e)
                    and fleet.current(e,time.monotonic())) or practice.suppresses(e['camera']):return
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
        service.armed=False;service.audio_allowed=False;service.speaking=False;service.last_audio=None
        service.camera_session=None;service.camera_focus=None


def calibration_source():
    return dict(worker=SESSION, generation=generation, motion_generation=motion_generation)


def invalidate_calibration():
    global room, motion_generation
    # A committed or uncertain commit must reload the complete durable pair.
    # Do not wait on the old Room's native job/lock before retiring its input.
    motion_generation += 1
    calibration_frames.clear()
    if room is not None:
        room.retire()
    room = None
    tracks.invalidate()
    pet_tracks.clear()
    identities.discard()
    clear_camera(SELECTED['id'])
    wall.clear_observation(SELECTED['id'])
    if camera_planner:
        camera_planner.views.clear()
    games.suspend_source()
    state.update(room=None, people=[], hands=[], animals=[], animals_current=False, _patrol_view=None,
                 tracking_current=False, scene={'objects': []})


calibration_saver = CalibrationSaver(CAMERA_STORAGE, invalidate_calibration)


def secondary_frame_current(observation, process):
    captured = observation.get('captured_mono')
    return (previews.source_current(observation['camera'], process)
            and type(captured) in (int, float) and 0 <= time.monotonic()-captured <= 1.5)


def close_primary_view():
    global motion_generation
    motion_generation += 1
    calibration_frames.clear()
    state.update(status='shutter_closed',reason='privacy_shutter_closed',people=[],hands=[],frame=None,
                 animals=[],animals_current=False,scene={'objects': []},_patrol_view=None)
    pet_tracks.clear()
    wall.clear(SELECTED['id'], primary_wall_epoch)
    clear_camera(SELECTED['id'])
    identities.discard()
    games.suspend_source()


async def primary_capture_closed(my_generation, wall_epoch, reason, error_class=None, *, stage='capture'):
    """Retire this source before waking its consumer or waiting for recovery."""
    global motion_generation, latest, last_frame, last_jpeg, last_preview, last_preview_at
    global primary_retired_scope
    if (my_generation != generation or wall_epoch != primary_wall_epoch
            or primary_retired_scope == (my_generation, wall_epoch)):
        return
    primary_retired_scope = (my_generation, wall_epoch)
    calibration_frames.clear()
    games.suspend_source()
    motion_generation += 1
    latest, last_frame, last_jpeg, last_preview, last_preview_at = None, 0, b'', b'', 0.0
    state.update(frame=None, people=[], hands=[], animals=[], animals_current=False, room=None,
                 scene={'objects': []}, _patrol_view=None)
    if state.get('status') != 'shutter_closed':
        state.update(status='unavailable', reason=reason, error_class=error_class, error_stage=stage)
    home.armed = False
    pet_tracks.clear()
    wall.clear(SELECTED['id'], wall_epoch)
    clear_camera(SELECTED['id'])
    identities.discard()
    for future in tuple(motor_waiters.values()):
        if not future.done():
            future.set_exception(RuntimeError(reason))
            # The dispatch coroutine may still be waiting for a room/privacy
            # lock and exit without awaiting this future. Awaiters still raise.
            future.exception()
    await notify()


def primary_frame_current(my_generation, frame_generation, received_at):
    return (my_generation == generation and frame_generation == motion_generation
            and state.get('status') in {'connecting', 'live'}
            and type(received_at) in (int, float) and 0 <= time.time()-received_at <= 1.5)


def scene_frame_current(my_generation, wall_epoch, item, frame_generation, current_item, *, warming=False):
    return (isinstance(item, dict) and isinstance(current_item, dict)
            and item.get('type') == current_item.get('type') == 'frame'
            and wall_epoch == primary_wall_epoch
            and primary_frame_current(my_generation, frame_generation,
                                      current_item.get('received_at') if warming else item.get('received_at'))
            and (warming or 0 <= time.time() - item['received_at'] <= 1.2)
            and item.get('_motion_generation') == frame_generation
            and current_item.get('_motion_generation') == frame_generation
            and isinstance(item.get('capture_session'), str)
            and item['capture_session'] == current_item.get('capture_session')
            and isinstance(item.get('camera_source_context'), str)
            and item['camera_source_context'] == current_item.get('camera_source_context'))


async def secondary_observation(observation, process):
    camera_id=observation['camera']
    if not secondary_frame_current(observation, process):return
    practice.observe(camera_id, observation['session'], observation['people'], observation['hands'],
        observation['registration'], observation['now'], observation['captured_mono'])
    safety.offer(camera_id, observation['session'], observation['people'], observation['now'],
        observation['captured_mono'], stable=observation.get('scene_visible', False)
            and observation['registration'].get('status') == 'locked',
        jpeg=observation.get('safety_jpeg', b''), aspect=observation.get('aspect', 16 / 9),
        source_context=observation.get('camera_source_context'),
        source_name=observation.get('camera_source_name'))
    if not secondary_frame_current(observation, process):return
    wall.observe(camera_id,observation['session'],observation['people'],
        time.time()-(time.monotonic()-observation['captured_mono']),
        hands=observation['hands'],
        moving=observation['registration'].get('status') in {'moving','settling','lost','stale'})
    active=interaction_enabled
    events=fleet.update(camera_id,observation['session'],observation['people'],observation['hands'],
        observation['registration'],active,observation['now'],captured_mono=observation['captured_mono'])
    await dispatch_interactions(events)
    if not secondary_frame_current(observation, process):return
    if camera_id not in secondary_homes:secondary_homes[camera_id]=HomeService(camera_id,voice_provider=home)
    service=secondary_homes[camera_id]
    if active and not service.enabled:await service.enable()
    if not secondary_frame_current(observation, process):return
    service.armed=active
    item=fleet.cameras.get(camera_id,{})
    now = time.monotonic()
    service.audio_allowed=fleet.voice(camera_id, now, service.attention.candidate() is not None,
        people=len(observation['people']), session=observation['session'],
        speaking=bool(service.speaking and service.last_audio is not None and 0 <= now-service.last_audio <= .6),
        enabled=active and service.armed and 0 <= now-observation['now'] <= 1.5
            and not (PRIVATE/'voice-input-paused').exists())
    await service.observe(observation['people'],observation['faces'],observation['registration'],
        observation['now'],process,active,item.get('owner'),gestures_enabled=False,
        sound_review=safety.sound_requested(camera_id),session_id=observation['session'],
        captured_mono=observation['captured_mono'])


async def secondary_audio(camera_id, packet):
    safety.observe_audio(camera_id, packet)
    service=secondary_homes.get(camera_id)
    if service:
        service.audio_allowed = service.audio_allowed and fleet.voice_current(camera_id, time.monotonic())
        await deliver_audio(service, packet)


previews = PreviewPool(identity_store=identity_store, continuous=True, recognize=True, wall=wall,
    on_observation=secondary_observation,on_audio=secondary_audio,on_clear=clear_camera,
    review_pending=safety.review_pending, fall_pending=safety.fall_pending,
    sound_cue=safety.sound_search_cue,
    focus=lambda cid:getattr(secondary_homes.get(cid),'camera_focus',None),
    occupied_elsewhere=lambda cid: (cid!=SELECTED['id'] and bool(view().get('people')))
        or any(other!=cid and previews.status(other)['state']=='live' and item.get('people',0)
               for other,item in previews.sessions.items()))
registry = RecognizerRegistry()
bindings = ActionBindings(registry)
configure(registry, bindings, games, home)


async def motor_step(direction, observed=None, expected_intent=None, decision_current=None):
    global motor_sequence, motion_generation, motor_last_sent
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
    owned_motion_generation = motion_generation
    owned_planner = camera_planner
    # Reserve before the first await. A second request must be recomputed after
    # the first completes; queuing it would retain an obsolete visual decision.
    if motor_waiters:
        raise RuntimeError('motor_busy')
    motor_sequence += 1
    request_id = motor_sequence
    future = asyncio.get_running_loop().create_future()
    motor_waiters[request_id] = future
    correlation = secrets.token_hex(16)
    began = time.monotonic()
    expires = min(began+1.5, observed+1.5) if observed is not None else began+1.5
    sent = False
    home.runtime_log.record('embodied.command','start',correlation=correlation)
    try:
        if not await camera_is_open(SELECTED):
            close_primary_view()
            raise RuntimeError('privacy_shutter_closed')
        async with room_lock:
            if not await camera_is_open(SELECTED):
                close_primary_view()
                raise RuntimeError('privacy_shutter_closed')
            # Registration and privacy reads may have waited. No await separates
            # the final authority/observation check from reserving and writing.
            if state.get('status')=='shutter_closed':raise RuntimeError('privacy_shutter_closed')
            if (capture is not owned_capture or camera_lease is not owned_lease
                    or not camera_lease.snapshot or camera_lease.snapshot.get('status')!='owned'):
                raise RuntimeError('camera_not_owned')
            if motion_generation != owned_motion_generation:
                raise RuntimeError('capture_source_changed')
            if expected_intent is not None and (camera_planner is not owned_planner
                    or not camera_planner or camera_planner.intent_revision != expected_intent):
                raise RuntimeError('camera_intent_changed')
            now = time.monotonic()
            if observed is not None and (not 0 <= now-observed <= 1.5
                    or camera_planner and camera_planner.mode=='hold'
                    or state.get('status')!='live' or state.get('tracking_current') is not True
                    or not 0 <= now-last_frame <= 1.5 or last_frame <= motor_last_sent
                    or decision_current is not None and not decision_current()):
                raise RuntimeError('stale_input')
            if now >= expires:
                raise RuntimeError('motor_request_expired')
            motion_generation += 1
            calibration_frames.clear()
            motor_last_sent = now
            state['_patrol_view'] = None
            state['scene'] = {'objects': []}
            identities.discard()
            fleet.clear(SELECTED['id'])
            home.selected = False
            home.epoch += 1
            home.vad = []
            home.pending_audio = None
            if room:
                room.expect_motion(now, 2)
            capture.stdin.write((json.dumps(dict(action='motor_step', direction=direction,
                request_id=request_id, expires_mono=expires))+'\n').encode())
            sent = True
        await capture.stdin.drain()
        result = await asyncio.wait_for(future, 3)
        if result.get('error_code') == 'motor_request_expired':
            raise RuntimeError('motor_request_expired')
        if result.get("error_class") or result.get("native_code") != 0:
            raise RuntimeError("local_motor_rejected")
        state["local_motor"] = dict(result, at=time.time())
        home.runtime_log.record('embodied.command','result',correlation=correlation,
            elapsed_ms=round((time.monotonic()-began)*1000))
        return result
    except asyncio.CancelledError:
        home.runtime_log.record('embodied.command','result',correlation=correlation,
            result='cancelled',code='control_cancelled_after_send' if sent else 'control_cancelled_before_dispatch',
            elapsed_ms=round((time.monotonic()-began)*1000))
        raise
    except Exception:
        home.runtime_log.record('embodied.command','result',correlation=correlation,
            result='unavailable',code='runtime_error',elapsed_ms=round((time.monotonic()-began)*1000))
        raise
    finally:
        motor_waiters.pop(request_id, None)
        if not future.done():future.cancel()


async def camera_search():
    global camera_lease, camera_planner
    io = Transport()
    lease = CameraLease(io, open_privacy=False, restore_native=True)
    camera_lease = lease
    try:
        await lease.acquire()
        planner = SearchPlanner(lease.presets, time.monotonic(), lease.active)
        intent = camera_intents.load()
        planner.mode, planner.intent_revision = intent.mode, intent.revision
        planner.intent_status = intent.status
        if os.environ.get("FRIDAY_CAMERA_HOLD")=="1":planner.mode="hold"
        camera_planner = planner
        director = CameraDirector()
        from homeplane.embodied.camera_follow_picker import CameraFollowPicker

        planner.follow_picker = CameraFollowPicker(director, lambda: primary_follow_sample(),
            lambda: (planner.mode, planner.intent_revision), lambda mode: set_primary_camera_mode(mode))
        checked = 0.0
        while True:
            now = time.monotonic()
            if now-checked >= 10:
                checked = now
                if not await camera_is_open(SELECTED):
                    close_primary_view()
                    return
            now = time.monotonic()
            sample = dict(state, _local_received=last_frame, game=games.public,
                party_camera=party_camera.status(camera=SELECTED['id'],session=state.get('session_id'),
                    frame_mono=last_frame,now=now))
            elsewhere = any(item.get('people',0) and previews.status(cid)['state']=='live'
                            for cid,item in previews.sessions.items())
            director.apply_mode(planner.mode, planner.intent_revision)
            direction = director.choose(sample, now, focus=getattr(home,'camera_focus',None),
                                        occupied_elsewhere=elsewhere,
                                        review_pending=safety.review_pending(SELECTED['id'], sample.get('session_id')),
                                        fall_pending=safety.fall_pending(SELECTED['id'], sample.get('session_id')),
                                        sound_cue=safety.sound_search_cue(SELECTED['id'], sample.get('session_id'),
                                                                         director.sound_search_cue)) if planner.mode!='hold' else None
            if planner.mode == 'hold':director.hold()
            state['camera_control'] = dict(planner.public(lease.presets), **director.public())
            if direction is not None:
                try:
                    def decision_current():
                        current = dict(state, _local_received=last_frame, game=games.public,
                            party_camera=party_camera.status(camera=SELECTED['id'],session=state.get('session_id'),
                                frame_mono=last_frame,now=time.monotonic()))
                        occupied = any(item.get('people',0) and previews.status(cid)['state']=='live'
                                       for cid,item in previews.sessions.items())
                        return director.decision_current(current, time.monotonic(), direction,
                            focus=getattr(home,'camera_focus',None), occupied_elsewhere=occupied,
                            review_pending=safety.review_pending(SELECTED['id'], current.get('session_id')),
                            fall_pending=safety.fall_pending(SELECTED['id'], current.get('session_id')),
                            sound_cue=safety.sound_search_cue(SELECTED['id'], current.get('session_id'),
                                                             director.sound_search_cue))
                    await motor_step(direction, sample['_local_received'], expected_intent=planner.intent_revision,
                                     decision_current=decision_current)
                    director.sent(time.monotonic())
                    lease.event('local_follow_impulse', direction=direction)
                except (RuntimeError, OSError, TimeoutError) as error:
                    # Capture connection is not ready during startup; retry only
                    # after a new fresh observation, with no queued motor commands.
                    if str(error) not in {'stale_input', 'camera_intent_changed', 'motor_busy', 'motor_request_expired', 'capture_source_changed'}:
                        state['camera_control']['phase'] = 'motor_unavailable'
                        director.failed(time.monotonic())
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
            # No queued motor command may run after vendor tracking is restored.
            try:
                await reap(capture)
            finally:
                restored = await lease.release()
                state["camera_control"] = {
                    "owner": "none",
                    "phase": "restored" if restored else "restore_pending",
                    "reason": "镜头已释放" if restored else "镜头待释放",
                }
        finally:
            try:await io.close()
            finally:camera_lease = None


def rate(times):
    return (
        round((len(times) - 1) / (times[-1] - times[0]), 1) if len(times) > 1 and times[-1] > times[0] else 0
    )


async def notify():
    async with changed:
        changed.notify_all()


def view():
    result = dict(state)
    result.pop('_patrol_view', None)
    result['party_camera'] = party_camera.status(camera=SELECTED['id'],session=state.get('session_id'),
        frame_mono=last_frame,now=time.monotonic())
    result["game"] = games.viewer_state()
    result["recording"] = recorder.public()
    result['safety_recording'] = safety.recording_status()
    result["home"] = home.public()
    result["voice_sessions"] = [dict(camera=cid,**{key:service.public().get(key) for key in ("enabled","voice","reason","last_transcript","last_event","voice_command","audio_input","audio_analysis")}) for cid,service in [(SELECTED["id"],home),*secondary_homes.items()]]
    result['interactions'] = fleet.public(time.monotonic())
    result['voice_input'] = not (PRIVATE/'voice-input-paused').exists()
    if camera_planner and result.get('camera_control'):
        control = result['camera_control']
        if control.get('intent_revision') != camera_planner.intent_revision:
            held = camera_planner.mode == 'hold'
            result['camera_control'] = dict(control,mode=camera_planner.mode,
                intent_revision=camera_planner.intent_revision,
                phase='held' if held else 'waiting_video',
                reason='停留当前视角' if held else '等待画面',view_usable=False,target_locked=False)
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
            game=dict(result['game'], status="paused", reason="实时画面已过期", players=[]),
        )
    if result['status'] != 'live' and result.get('camera_control'):
        result['camera_control'] = dict(result['camera_control'], view_usable=False, target_locked=False)
    if result['status'] != 'live':
        result['game'] = dict(result['game'], players=[])
        if result['game'].get('mode') != 'off' and result['game'].get('status') != 'complete':
            result['game'].update(status='paused', reason='实时画面已过期')
        if result['game']['source_recovery']['required']:
            result['game']['source_recovery'] = dict(result['game']['source_recovery'],ready=False,phase='camera')
    pet_current = (result['status'] == 'live' and camera_planner and camera_planner.mode in PET_MODES
                   and result.get('animals_intent') == camera_planner.intent_revision
                   and result.get('animals_current') is True)
    if not pet_current:
        result.update(animals=[],animals_current=False)
        if result.get('pet_tracking'):result['pet_tracking'] = dict(result['pet_tracking'],current=False)
    result['camera_sessions'] = [dict(camera=SELECTED['id'],state=result['status'],recognition='running' if result['status']=='live' else 'waiting',frames=state['processed_frames']),
        *[previews.status(c['id']) for c in configuration()['cameras'] if c['id']!=SELECTED['id']]]
    return result


def authorized(client, key):
    return bool(client and client.host in ALLOWED_IPS and secrets.compare_digest(key or "", KEY))


async def reap(process):
    if process and process.returncode is None:
        try:process.terminate()
        except ProcessLookupError:pass
        try:
            await asyncio.wait_for(process.wait(), 3)
        except asyncio.TimeoutError:
            try:process.kill()
            except ProcessLookupError:pass
            await process.wait()


async def run_source(my_generation):
    global capture, vision, latest, last_frame, tracks, pet_tracks, last_jpeg, room, primary_wall_epoch
    global last_preview, last_preview_at
    primary_wall_epoch = wall.begin(SELECTED['id'])
    wall_epoch = primary_wall_epoch
    identities.session = f"{SESSION}_{my_generation}_{secrets.token_hex(3)}"
    state["session_id"] = identities.session
    games.begin_source()
    source_scope = object()
    room_ok = False
    if room is not None:room.retire()
    room = None
    home.armed = False
    home.selected = False
    home.epoch += 1
    home.vad = []
    home.pending_audio = None
    fleet.clear(SELECTED['id'])
    home.gesture.reset()
    tracks = Tracks()
    pet_tracks = PetTracks()
    processed_times.clear()
    captured_times.clear()
    state.update(
        status="connecting",
        reason=None,
        frame=None,
        people=[],
        hands=[],
        animals=[],animals_current=False,animals_intent=None,pet_tracking=pet_tracks.public(),
        room=None,
        _patrol_view=None,
        game={"mode": games.mode, "status": "paused", "reason": "实时画面已过期", "players": []},
    )
    camera_task = None
    stage = "source_start"
    try:
        if not await camera_is_open(SELECTED):
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
        capture_session = secrets.token_hex(16)
        audio_ingress = AudioIngress(SELECTED, capture_session)
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
                     "audio": True, "identity": False, "capture_session": capture_session}
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
            detector = SceneProcess(MODELS / "yolov8n.onnx")
            processed_at = None
            try:
                while my_generation == generation:
                    item = latest_item
                    frame_generation = item.get('_motion_generation') if isinstance(item, dict) else None
                    current = lambda i=item, m=frame_generation: scene_frame_current(
                        my_generation, wall_epoch, i, m, latest_item)
                    key = ((item['received_at'], item['capture_session'],
                            item['camera_source_context'], frame_generation) if current() else None)
                    if key is not None and key != processed_at:
                        processed_at = key
                        try:
                            result = await detector.analyze(base64.b64decode(item['jpeg'], validate=True),
                                context=dict(generation=my_generation, wall_epoch=wall_epoch,
                                    motion_generation=frame_generation, capture_session=item['capture_session'],
                                    camera_source_context=item['camera_source_context']), current=current,
                                active=lambda i=item, m=frame_generation: scene_frame_current(
                                    my_generation, wall_epoch, i, m, latest_item, warming=True))
                        except Exception:
                            result = None
                        if result is not None and current():
                            source = {k: item[k] for k in ('type', 'received_at', '_motion_generation',
                                                          'capture_session', 'camera_source_context')}
                            scene_data = dict(result, observed_at=item['received_at'], _source=source)
                    if not scene_frame_current(my_generation, wall_epoch, scene_data.get('_source'),
                                               frame_generation, latest_item):
                        scene_data = {'objects': [], 'observed_at': 0}
                    if my_generation == generation:
                        state['scene_service'] = detector.status()
                    await asyncio.sleep(0.7)
            finally:
                scene_data = {'objects': [], 'observed_at': 0}
                await detector.close()
                if my_generation == generation:
                    state['scene_service'] = detector.status()

        last_video_lease=0.0
        async def receive():
            nonlocal latest_item,last_video_lease
            reason, error_class = None, None
            try:
                while raw := await capture.stdout.readline():
                    if my_generation != generation:return
                    item = json.loads(raw)
                    if not isinstance(item, dict):
                        raise ValueError('invalid_capture_packet')
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
                        admission = (audio_ingress.reject('camera_paused') if state.get('status') == 'shutter_closed'
                            else audio_ingress.accept(item,wall=time.time(),mono=time.monotonic()))
                        safety.observe_audio(SELECTED['id'], admission)
                        await deliver_audio(home,admission)
                        continue
                    if item.get("type") == "motor_result":
                        future = motor_waiters.get(item.get("request_id"))
                        if future and not future.done():
                            future.set_result(item)
                        continue
                    if item.get('type')=='video':
                        if state.get('status')!='shutter_closed':wall.offer(SELECTED['id'],wall_epoch,item)
                        continue
                    if item.get("type") == "frame":
                        # Bind queued pixels to the motion epoch at receipt, before any await.
                        item['_motion_generation'] = motion_generation
                    latest_item = item
                    if item.get("type") == "frame":
                        audio_ingress.observe_frame(item,wall=time.time(),mono=time.monotonic())
                        if time.monotonic()-last_video_lease>.7:
                            last_video_lease=time.monotonic()
                            capture.stdin.write((json.dumps(dict(action='video_window',enabled=wall.video[SELECTED['id']].subscribers>0))+'\n').encode())
                        captured_times.append(time.monotonic())
                        if state.get('status') != 'shutter_closed':wall.offer(SELECTED['id'], wall_epoch, item)
                    offer_frame(queue, item)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                reason, error_class = 'capture_stream_unavailable', type(error).__name__
            else:
                reason = 'source_closed'
            finally:
                if reason is not None and my_generation == generation:
                    latest_item = None
                    try:
                        await primary_capture_closed(my_generation, wall_epoch, reason, error_class)
                    finally:
                        if my_generation == generation and wall_epoch == primary_wall_epoch:
                            offer_frame(queue, dict(type='error', reason=reason, error_class=error_class))

        receiver = asyncio.create_task(receive())
        scene_task = asyncio.create_task(observe_scene())
        try:
            while my_generation == generation:
                if camera_task and camera_task.done():
                    if state['status']=='shutter_closed':return
                    raise RuntimeError('camera_owner_unavailable')
                stage = "waiting_frame"
                item = await asyncio.wait_for(queue.get(), CAPTURE_RECEIVE_TIMEOUT)
                if item["type"] != "frame":
                    if state['status'] == 'shutter_closed':return
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
                frame_motion_generation = item.get('_motion_generation', motion_generation)
                if not primary_frame_current(my_generation, frame_motion_generation, item['received_at']):
                    continue
                phase_at = time.monotonic()
                timings = dict(queue_age_ms=round((time.time()-item['received_at'])*1000,1))
                # Pixel data remains exclusively in local process pipes and memory.
                frame_scene = scene_data
                current_scene = ({k: frame_scene[k] for k in ('objects', 'scene_ms', 'observed_at')}
                    if scene_frame_current(my_generation, wall_epoch, frame_scene.get('_source'),
                                           frame_motion_generation, item) else {'objects': []})
                if room is not None and room.reg_info.get("status") in ("moving", "settling"):
                    current_scene = {"objects": []}
                rois = [
                    o["box"]
                    for o in current_scene["objects"]
                    if o["class"] == "person" and o["confidence"] >= 0.5
                ]
                pet_owner = camera_planner
                pet_revision = pet_owner.intent_revision if pet_owner and pet_owner.mode in PET_MODES else None
                stage = "vision_request"
                detection = await request_vision(vision, {
                    "jpeg": item["jpeg"],
                    "face_jpeg": item.get("face_jpeg"),
                    "person_rois": rois,
                    "hand_interval_ms": 140 if games.mode not in ("off", "home") else 80,
                    "attention_enabled": True,
                    "animals_enabled": pet_revision is not None,
                })
                timings['vision_ms'] = round((time.monotonic()-phase_at)*1000,1)
                phase_at = time.monotonic()
                if my_generation != generation:
                    break
                if state.get('status') == 'shutter_closed':return
                if not primary_frame_current(my_generation, frame_motion_generation, item['received_at']):
                    state['pipeline_timing'] = dict(timings, dropped_stage='after_vision')
                    safety.clear(SELECTED['id'])
                    fleet.clear(SELECTED['id']);home.selected=False;home.epoch+=1;identities.discard()
                    continue
                now = time.monotonic()
                picture = base64.b64decode(item["jpeg"])
                if room is None:
                    room = RoomSession(CAMERA_STORAGE, scope=source_scope)
                hands = detection.get("hands", [])
                async with room_lock:
                    frame_room = room
                    frame_current = lambda: primary_frame_current(my_generation, frame_motion_generation, item['received_at'])
                    if not primary_frame_current(my_generation, frame_motion_generation, item['received_at']):
                        continue
                    masks = [{"box": b} for b in detection.get("boxes", [])]
                    masks.extend({'box': row['box']} for row in PetTracks.read_rows(detection) or [])
                    registered = await frame_room.register(picture, masks, now, current=frame_current,
                        active=lambda: scene_frame_current(my_generation, wall_epoch, item,
                            frame_motion_generation, latest_item, warming=True))
                    if not primary_frame_current(my_generation, frame_motion_generation, item['received_at']):
                        continue
                    people = tracks.update(detection, now, transform=frame_room.registration,
                        camera_moving=not registered or now < frame_room.command_until or frame_room.reg_info.get('status') in {'moving','settling'})
                    pet_active = (pet_revision is not None and camera_planner is pet_owner
                        and pet_owner.mode in PET_MODES and pet_owner.intent_revision == pet_revision)
                    animals = pet_tracks.update(detection, now, intent=pet_revision if pet_active else None,
                        transform=frame_room.registration,
                        camera_moving=not registered or now < frame_room.command_until or frame_room.reg_info.get('status') in {'moving','settling'})
                    if not scene_frame_current(my_generation, wall_epoch, frame_scene.get('_source'),
                                               frame_motion_generation, item):
                        current_scene = {'objects': []}
                    layout = (await frame_room.update(picture, people, hands, now, item['width'], item['height'],
                        current_scene['objects'], current=frame_current)) if registered else None
                    if layout is None:
                        if room_ok:games.suspend_source()
                        room_ok=False
                        tracks.invalidate();pet_tracks.clear();identities.discard()
                        people,hands,animals=[],[],[]
                        layout=unavailable_layout()
                    elif not room_ok:
                        games.begin_source()
                        room_ok=True
                    state['room_service']=frame_room.status()
                if not primary_frame_current(my_generation, frame_motion_generation, item['received_at']):
                    if my_generation != generation:return
                    state['pipeline_timing'] = dict(timings, dropped_stage='after_layout')
                    continue
                if layout["registration"]["status"] in ("moving", "settling"):
                    for p in people:
                        p["motion"] = "camera_moving"
                    pet_tracks.clear()
                    animals = []
                timings['layout_ms'] = round((time.monotonic()-phase_at)*1000,1)
                phase_at = time.monotonic()
                identity_rows = await analyze(
                    identities, base64.b64decode(item.get("face_jpeg") or item["jpeg"]), people,
                    detection.get("faces", []), moving=now < frame_room.command_until or layout["registration"]["status"] in ("moving", "settling"),
                    sample=bool(item.get('face_jpeg')) or max(item.get('native_size') or [0])<=1920,
                    captured_at=time.monotonic()-(time.time()-item['received_at']))
                timings['identity_ms'] = round((time.monotonic()-phase_at)*1000,1)
                phase_at = time.monotonic()
                scene_visible = frame_room.scene_visible
                timings['visibility_ms'] = round((time.monotonic()-phase_at)*1000,1)
                state['pipeline_timing'] = dict(timings)
                if state.get('status')=='shutter_closed':return
                if not primary_frame_current(my_generation, frame_motion_generation, item['received_at']):
                    if my_generation != generation:return
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
                safety.offer(SELECTED['id'], identities.session, people, now,
                    time.monotonic() - (time.time() - item['received_at']),
                    stable=scene_visible and layout['registration'].get('status') == 'locked'
                        and now >= frame_room.command_until,
                    jpeg=picture, aspect=item['width'] / max(1, item['height']),
                    source_context=item.get('camera_source_context'), source_name=item.get('camera_source_name'))
                if not primary_frame_current(my_generation, frame_motion_generation, item['received_at']):
                    if my_generation != generation:return
                    state['pipeline_timing'] = dict(timings, dropped_stage='after_safety')
                    continue
                wall.observe(SELECTED['id'],identities.session,people,item['received_at'],
                    hands=hands,
                    moving=layout['registration'].get('status') in {'moving','settling','lost','stale'})
                if practice.suppresses(SELECTED['id']):
                    # Practising OK must neither join nor start a game. Keep its
                    # latch closed until the person releases after the lesson.
                    games.gesture_latched = True
                    games.gesture_last = now
                    games.gesture_release = None
                    game = games.public
                else:
                    game = games.update(people, layout["registration"], now, hands,
                                        captured_at=time.monotonic()-(time.time()-item['received_at']))
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
                            current=lambda e=event, g=my_generation, m=frame_motion_generation, at=item['received_at']: (
                                primary_frame_current(g, m, at)
                                and interaction_enabled
                                and not practice.suppresses(SELECTED['id'])
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
                if not primary_frame_current(my_generation, frame_motion_generation, item['received_at']):
                    if my_generation != generation:return
                    state['pipeline_timing'] = dict(timings, dropped_stage='after_enable')
                    continue
                last_selection = games.selected_mode
                game_window = (games.control_context, games.control_revision)
                party_window=party_camera.window(time.monotonic())
                gesture_active = interaction_enabled and games.mode in {'off', 'home'}and not party_window[2]
                practice.observe(SELECTED['id'], identities.session, people, hands,
                    layout['registration'], now, now-(time.time()-item['received_at']))
                interaction_events=fleet.update(SELECTED['id'],identities.session,people,hands,
                    layout['registration'],gesture_active,now,game_window=(game_window,party_window[:2]),
                    game_active=games.mode not in {'off','home'}or party_window[2],
                    captured_mono=time.monotonic()-(time.time()-item['received_at']))
                for event in interaction_events:
                    event['_game_window'] = game_window
                    event['_party_window'] = party_window[:2]
                voice_now = time.monotonic()
                voice_captured_mono = voice_now - (time.time() - item['received_at'])
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
                    sound_review=safety.sound_requested(SELECTED['id']),
                    session_id=identities.session,
                    captured_mono=voice_captured_mono,
                )
                if state.get('status')=='shutter_closed':return
                if not primary_frame_current(my_generation, frame_motion_generation, item['received_at']):
                    if my_generation != generation:return
                    state['pipeline_timing'] = dict(timings, dropped_stage='after_home')
                    continue
                await dispatch_interactions(interaction_events)
                if not primary_frame_current(my_generation, frame_motion_generation, item['received_at']):
                    if my_generation != generation:return
                    state['pipeline_timing'] = dict(timings, dropped_stage='after_dispatch')
                    continue
                timings['interaction_ms'] = round((time.monotonic()-phase_at)*1000,1)
                state['pipeline_timing'] = dict(timings)
                processed_times.append(time.monotonic())
                if item.get("preview"):
                    last_preview = base64.b64decode(item["preview"])
                    last_preview_at = item["preview_at"]
                last_jpeg = picture
                last_frame = time.monotonic()-(time.time()-item['received_at'])
                if not scene_frame_current(my_generation, wall_epoch, frame_scene.get('_source'),
                                           frame_motion_generation, item):
                    current_scene = {'objects': []}
                calibration_frames.publish(frame_room.draft, picture,
                    dict(width=item['width'],height=item['height'],room=layout,people=[],hands=[],status='live'),
                    calibration_source(),item['received_at'])
                state.update(
                    status="live",
                    tracking_current=tracks.input_valid,
                    animals=animals,animals_current=pet_tracks.current,
                    animals_intent=pet_revision if pet_active else None,pet_tracking=pet_tracks.public(),
                    scene_visible=scene_visible,
                    _patrol_view=getattr(frame_room, 'patrol_view', None) if registered else None,
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
                    animal_ms=round(detection.get("animal_ms", 0), 1),
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
            await primary_capture_closed(my_generation, wall_epoch, 'source_or_detector_unavailable',
                                         type(error).__name__, stage=stage)
    finally:
        try:
            await primary_capture_closed(my_generation, wall_epoch, 'source_closed')
        finally:
            wall.clear(SELECTED['id'], wall_epoch)
            safety.clear(SELECTED['id'])
            try:
                state['identity_cleanup_pending'] = await close_identity(identities) is False
            finally:
                try:
                    try:
                        await close_rooms(source_scope)
                    finally:
                        if camera_task:
                            camera_task.cancel()
                            await asyncio.gather(camera_task, return_exceptions=True)
                finally:
                    try:await reap(capture)
                    finally:
                        try:await reap(vision)
                        finally:capture = vision = None


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
        _patrol_view=None,
        game={"mode": games.mode, "status": "paused", "reason": "实时画面已过期", "players": []},
    )
    await notify()


@asynccontextmanager
async def lifespan(app):
    await safety.start()
    if os.environ.get("FRIDAY_START_IDLE") != "1":
        await start()
    else:
        state["camera_control"] = {"owner": "none", "phase": "idle", "reason": "识别尚未启动"}
    yield
    try:
        await calibration_saver.close()
    finally:
        await stop()
    await previews.close()
    await home.close()
    await safety.close()
    identity_store.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.get('/api/safety/status')
async def safety_status():
    return safety.status()


@app.post('/api/safety/policy')
async def safety_policy_changed():
    await safety.sync_policy()
    return {'revision': safety.policy_revision}


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


@app.get("/assets/{name:path}")
async def asset(name: str, request: Request):
    from .game_assets import game_asset_path
    path = game_asset_path(name)
    if path is None:
        raise HTTPException(404)
    return static_asset(path,request.headers)


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
        if paused:
            await safety.pause_sound()
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
        report = view()
        return {
            "state": report["status"],
            "camera": camera_id,
            "frames": state["processed_frames"],
            "primary": True,
            "camera_control": report.get('camera_control'),
            "pet_tracking": report.get('pet_tracking'),
            "follow_target_available": report['status'] == 'live' and (report.get('camera_control') or {}).get('owner') == 'friday',
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


def primary_follow_sample():
    if capture is None or capture.returncode is not None:
        return {}
    now = time.monotonic()
    return dict(state, _local_received=last_frame, game=games.public,
                _follow_motion=motion_generation, _follow_stable=not motor_waiters and last_frame > motor_last_sent,
                party_camera=party_camera.status(camera=SELECTED['id'], session=state.get('session_id'),
                                                 frame_mono=last_frame, now=now))


def follow_picker(camera_id, request):
    if request.client.host not in {'127.0.0.1', '::1'}:
        raise HTTPException(403)
    origin = request.headers.get('origin')
    if origin and origin != 'http://' + request.headers.get('host', ''):
        raise HTTPException(403)
    if camera_id != SELECTED['id']:
        return previews.follow_picker(camera_id)
    if (camera_planner is None or camera_lease is None or not camera_lease.snapshot
            or camera_lease.snapshot.get('status') != 'owned'):
        raise ValueError('follow_target_unavailable')
    return camera_planner.follow_picker


@app.get('/api/previews/{camera_id}/follow-target')
async def follow_target_view(camera_id: str, request: Request):
    try:
        result = follow_picker(camera_id, request).snapshot()
    except ValueError:
        raise HTTPException(409, detail='follow_target_unavailable') from None
    return JSONResponse(result, headers={'Cache-Control': 'no-store'})


@app.post('/api/previews/{camera_id}/follow-target')
async def follow_target_command(camera_id: str, command: FollowCommand, request: Request):
    try:
        return follow_picker(camera_id, request).command(command)
    except (ValueError, RuntimeError, OSError):
        raise HTTPException(409, detail='follow_target_unavailable') from None


@app.post('/api/party-camera')
async def party_camera_control(request: Request):
    try:
        return party_camera.command(await request.json(),dict(view(),_local_received=last_frame),
            time.monotonic(),motor_busy=bool(motor_waiters),motor_last_sent=motor_last_sent)
    except ValueError as error:
        raise HTTPException(409,detail=str(error))from None


@app.get('/api/practice/capabilities')
async def practice_capabilities():
    return practice.capabilities()


@app.post('/api/practice/start')
async def practice_start(request: Request):
    data = await request.json()
    context = {}
    if data.get('request_id') is not None:
        try:context = practice.request_context(data['request_id'],data.get('expected_scope'))
        except ValueError:raise HTTPException(409,detail='practice_source_changed') from None
    if games.mode not in {'off', 'home'}:
        return dict(phase='blocked', reason='game_active',**context)
    if not any(s['state'] == 'live' for s in view()['camera_sessions']):
        return dict(phase='blocked', reason='camera_not_ready',**context)
    try:
        if context:
            return practice.begin_request(data.get('gesture'),[c['id'] for c in configuration()['cameras']],
                data['request_id'],data.get('expected_scope'),data.get('not_after'),data.get('lease_deadline'))
        return practice.begin(data.get('gesture'), [c['id'] for c in configuration()['cameras']])
    except ValueError:
        raise HTTPException(409, detail='practice_not_ready') from None


@app.post('/api/practice/status')
async def practice_status(request: Request):
    data = await request.json()
    if data.get('request_id') is not None:
        try:return practice.status_request(data['request_id'],data.get('expected_scope'))
        except ValueError:raise HTTPException(409,detail='practice_source_changed') from None
    if not isinstance(data.get('token'), str):
        raise HTTPException(422)
    return practice.status(data['token'])


@app.post('/api/practice/stop')
async def practice_stop(request: Request):
    data = await request.json()
    if data.get('request_id') is not None:
        try:return practice.stop_request(data['request_id'],data.get('expected_scope'))
        except ValueError:raise HTTPException(409,detail='practice_source_changed') from None
    if not isinstance(data.get('token'), str):
        raise HTTPException(422)
    return practice.stop(data['token'])


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
    origin = request.headers.get('origin')
    if origin and origin != 'http://' + request.headers.get('host', ''):
        raise HTTPException(403)
    data = await request.json()
    if not isinstance(data, dict):raise HTTPException(400)
    direction, mode = data.get('direction'), data.get('mode')
    if bool(direction)==bool(mode):raise HTTPException(400)
    if camera_id == SELECTED['id']:
        if direction:
            if not camera_planner:raise HTTPException(409, detail='camera_not_owned')
            return await manual_motor(request)
        return await camera_control(request)
    try:
        return await previews.control(camera_id,direction=direction,mode=mode)
    except ValueError:raise HTTPException(400) from None
    except (RuntimeError,OSError,TimeoutError):
        raise HTTPException(409,detail='motor_not_ready') from None


@app.get("/")
async def index():
    return HTMLResponse(static_page(HERE / 'static/index.html'),headers={'Cache-Control':'no-cache'})


@app.get("/games")
async def game_page():
    return HTMLResponse(static_page(HERE / 'static/games.html'),headers={'Cache-Control':'no-cache'})


@app.post('/api/game/presentation')
async def game_presentation(request: Request):
    origin = request.headers.get('origin')
    if origin and origin != 'http://' + request.headers.get('host', ''):
        raise HTTPException(403)
    try:
        return games.observe_presentation(await request.json())
    except (TypeError, ValueError):
        raise HTTPException(409, detail='game_view_changed') from None


@app.post("/api/game")
async def game_control(request: Request):
    origin = request.headers.get("origin")
    if origin and origin != "http://" + request.headers.get("host", ""):
        raise HTTPException(403)
    data = await request.json()
    if not isinstance(data, dict):
        raise HTTPException(400, detail='invalid_game_request')
    action = data.get('action')
    if action is not None and (not isinstance(action, str) or action not in {'select','prepare','start','pause','resume'}):
        raise HTTPException(400, detail='invalid_game_action')
    if set(data)-{'mode','action','difficulty','ski_trail','tennis_variant','expected_context','expected_revision'}:
        raise HTTPException(400, detail='invalid_game_request')
    if action == 'prepare' and (data.get('mode') != 'tennis' or 'tennis_variant' not in data):
        raise HTTPException(400, detail='invalid_tennis_variant')
    if 'difficulty' in data and (type(data['difficulty']) is not str
            or action not in {'select','prepare','start'} or data.get('mode') not in {'quest','ski','tennis','hiit'}):
        raise HTTPException(400, detail='invalid_game_difficulty')
    if 'ski_trail' in data and (type(data['ski_trail']) is not str or action != 'select' or data.get('mode') != 'ski'):
        raise HTTPException(400, detail='invalid_ski_trail')
    if 'tennis_variant' in data and (type(data['tennis_variant']) is not str or action not in {'select', 'prepare'}
                                     or data.get('mode') != 'tennis'):
        raise HTTPException(400, detail='invalid_tennis_variant')
    if not games.control_current(data.get('expected_context'),data.get('expected_revision')):
        raise HTTPException(409, detail='game_state_changed')
    if practice.active() and action != 'pause' and data.get("mode", "quest") not in {"off", "home"}:
        raise HTTPException(409, detail="practice_active")
    try:
        if action in {'pause', 'resume'}:
            if data.get('mode') != games.mode:
                raise ValueError('game_changed')
            games.set_paused(action == 'pause')
        elif action in {'select', 'prepare'}:
            options = {key: data[key] for key in ('difficulty', 'ski_trail', 'tennis_variant') if key in data}
            if action == 'prepare':
                options['replace_round'] = True
            games.select(data.get("mode", "quest"), **options)
        else:
            games.require_game_view(data.get('mode', 'off'))
            games.reset(data.get("mode", "off"), **({'difficulty':data['difficulty']} if 'difficulty' in data else {}))
        if data.get("action") == "start" and games.mode != "off":
            await start()
        if action not in {'pause','resume'} and games.selected_mode == "home" and data.get("mode") != "off":
            await home.enable()
            await start()
    except ValueError as error:
        if str(error) in {'game_input_not_ready','game_presentation_not_ready'}:
            raise HTTPException(409, detail=str(error))
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
    if state.get('status')!='live':raise HTTPException(409,detail='等待画面稳定后再校准')
    try:return calibration_frames.open(calibration_source())
    except ValueError:raise HTTPException(409,detail='等待画面稳定后再校准')from None


@app.post("/api/calibration")
async def save_calibration(request: Request):
    if request.client.host not in {"127.0.0.1", "::1"}:
        raise HTTPException(403)
    origin = request.headers.get("origin")
    if origin and origin != "http://" + request.headers.get("host", ""):
        raise HTTPException(403)
    try:
        if int(request.headers.get('content-length', '0')) > 3_000_000:
            raise HTTPException(413)
        body = bytearray()
        async for part in request.stream():
            if len(body) + len(part) > 3_000_000:
                raise HTTPException(413)
            body.extend(part)
        data = json.loads(body)
        config = dict(data['config'])
        source = config.pop('_source', None)
        reference = base64.b64decode(data["reference_jpeg"], validate=True)
        reference = calibration_frames.resolve(source,reference,calibration_source())
        result = await calibration_saver.save(config, reference, current=lambda: (
            calibration_frames.valid(source,calibration_source()) and state.get('status') == 'live'))
    except HTTPException:
        raise
    except Exception as error:
        reason = str(error)
        code, message = (409, '画面已更新，请重新校准') if reason == 'calibration_source_changed' else (
            (409, '正在保存，请稍候') if reason == 'calibration_busy' else (
            (503, '暂时无法保存，请稍后重试') if reason == 'calibration_unavailable' else
            (400, '请检查标记位置和参考画面')))
        raise HTTPException(code, detail=message) from None
    return result


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
    if not isinstance(data, dict):raise HTTPException(400)
    mode = data.get("mode")
    if mode not in ("auto", "pets", "hold", "survey"):
        raise HTTPException(400)
    if not camera_planner:
        raise HTTPException(409, detail="camera_not_owned")
    try:
        set_primary_camera_mode(mode)
    except (OSError, ValueError, RuntimeError):
        raise HTTPException(409, detail='camera_control_unavailable') from None
    if mode == "survey":
        camera_planner.visited.clear()
        camera_planner.last_person = time.monotonic() - 8
        camera_planner.last_move = time.monotonic() - 12
    return {"mode": mode, "intent_revision": camera_planner.intent_revision}


def set_primary_camera_mode(mode):
    if mode not in CAMERA_MODES:
        raise ValueError('invalid_camera_mode')
    if not camera_planner:
        raise RuntimeError('camera_not_owned')
    camera_planner.mode = 'hold'
    camera_planner.intent_revision = secrets.token_hex(16)
    camera_planner.intent_status = 'unavailable'
    intent = camera_intents.save(mode)
    camera_planner.mode = intent.mode
    camera_planner.intent_revision = intent.revision
    camera_planner.intent_status = intent.status


@app.post("/api/motor")
async def manual_motor(request: Request):
    if request.client.host not in {"127.0.0.1", "::1"}:
        raise HTTPException(403)
    origin = request.headers.get("origin")
    if origin and origin != "http://" + request.headers.get("host", ""):
        raise HTTPException(403)
    data = await request.json()
    if not isinstance(data, dict) or data.get('direction') not in ('left', 'right', 'up', 'down'):
        raise HTTPException(400)
    try:
        set_primary_camera_mode('hold')
        return await motor_step(data.get("direction"))
    except ValueError:
        raise HTTPException(400)
    except (OSError, RuntimeError):
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
            room.reset_seats()
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
    from homeplane.embodied.camera_guardian import check_guardian, watch_owner
    check_guardian()
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
    parent = int(os.environ['FRIDAY_SERVICE_OWNER_PID'])

    watch = asyncio.create_task(watch_owner(parent, lambda: setattr(server, 'should_exit', True)))
    try:
        await server.serve()
    finally:
        watch.cancel()
        await asyncio.gather(watch, return_exceptions=True)


if __name__ == "__main__":
    from homeplane.embodied.camera_guardian import exit_with_owner
    exit_with_owner(int(os.environ.get('FRIDAY_SERVICE_OWNER_PID', '0')))
    asyncio.run(serve())
