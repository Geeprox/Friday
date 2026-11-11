"""Owned camera sessions: bounded previews, identity and whole-home recognition."""

import asyncio
import base64
import json
import secrets
import time

from .paths import DATA, MODELS, PROJECT, PYTHON, camera, camera_storage, capture_environment
from .camera_director import CameraDirector
from .camera_follow_picker import CameraFollowPicker
from .camera_intent import CAMERA_MODES, PET_MODES, TARGET_MODES, CameraIntentStore
from .camera_audio import AudioIngress
from .frame_sampling import CAPTURE_IDLE_TIMEOUT, CAPTURE_RECEIVE_TIMEOUT, offer_frame
from homeplane.runtime_log import RuntimeLog
from homeplane.people.live import close_identity


async def camera_is_open(selected, *, client_factory=None, parameter_factory=None):
    """Read privacy state without changing it; failed reads remain unavailable."""
    from .miot_spec import camera_property

    siid, piid = camera_property(selected, 'camera_power', 'read')
    if client_factory is None:
        from miot.cloud import MIoTHttpClient
        client_factory = MIoTHttpClient
    if parameter_factory is None:
        from miot.types import MIoTGetPropertyParam
        parameter_factory = MIoTGetPropertyParam
    from .paths import PRIVATE
    from .miot_session import MiSession
    login = await MiSession(PRIVATE).current()
    api = client_factory(cloud_server=selected.get('region', 'cn'), access_token=login["access_token"])
    try:
        rows = await asyncio.wait_for(api.get_props_async(
            [parameter_factory(did=selected["device"]["did"], siid=siid, piid=piid)]), 12)
    finally:
        await api.deinit_async()
    rows = [row for row in rows if isinstance(row, dict)
            and str(row.get('did')) == str(selected['device']['did'])
            and (row.get('siid'), row.get('piid')) == (siid, piid)]
    if len(rows) != 1 or rows[0].get("code") != 0 or type(rows[0].get("value")) is not bool:
        raise RuntimeError("camera_state_unavailable")
    return rows[0]["value"]


class PreviewPool:
    def __init__(self, identity_store=None, continuous=False, *, recognize=False, control_camera=False,
                 on_observation=None, on_audio=None, on_clear=None, focus=None, occupied_elsewhere=None,
                 review_pending=None, fall_pending=None, sound_cue=None, wall=None, intent_factory=None):
        self.wall = wall
        self.sessions = {}
        self.focus = focus or (lambda camera: None)
        self.occupied_elsewhere = occupied_elsewhere or (lambda camera: False)
        self.review_pending = review_pending or (lambda camera, session: False)
        self.fall_pending = fall_pending or (lambda camera, session: False)
        self.sound_cue = sound_cue or (lambda camera, session, active: None)
        self.runtime_log = RuntimeLog(DATA / "logs" / "camera-motion")
        self.lock = asyncio.Lock()
        self.identity_store = identity_store
        self.continuous = continuous
        self.recognize = recognize
        self.control_camera=control_camera
        self.on_observation, self.on_audio, self.on_clear = on_observation, on_audio, on_clear
        self.intent_factory = intent_factory or (lambda selected: CameraIntentStore(
            camera_storage(selected['id']), selected))

    async def start(self, camera_id):
        selected = camera(camera_id)
        async with self.lock:
            old = self.sessions.get(camera_id)
            if old and not old["task"].done():
                old["expires"] = time.monotonic() + 60
                return self.status(camera_id)
            intent = self.intent_factory(selected).load(default='auto' if self.recognize else 'hold')
            entry = dict(
                state="connecting",
                frame=None,
                frames=0,
                received_at=0,
                # The native transport may need its bounded 65s reconnect on a
                # cold camera. Keep the first preview alive through that window.
                expires=time.monotonic() + 90,
                process=None,
                recognizer=None,
                shutter_changed=False,
                mode=intent.mode if self.recognize else 'hold',
                mode_revision=intent.revision,
                intent_status=intent.status,
            )
            self.sessions[camera_id] = entry
            entry["task"] = asyncio.create_task(self._monitor(selected, entry))
            return self.status(camera_id)

    def status(self, camera_id):
        item = self.sessions.get(camera_id)
        if not item:
            return dict(state="stopped", camera=camera_id, frames=0)
        fresh = time.time() - item["received_at"] < 2
        control = item.get('camera_control')
        if control:
            control = dict(control, mode=item.get('mode', control.get('mode')),
                           intent_status=item.get('intent_status', 'default'))
            if item.get('camera_control',{}).get('intent_revision') != item.get('mode_revision'):
                held = item.get('mode') == 'hold'
                control.update(phase='held' if held else 'waiting_video',
                    reason='停留当前视角' if held else '等待画面',view_usable=False,target_locked=False,
                    intent_revision=item.get('mode_revision'))
        if control and (not fresh or item['state']!='live'):
            control = dict(control,view_usable=False,target_locked=False)
        return dict(
            state=item["state"] if fresh or item["state"] != "live" else "stale",
            camera=camera_id,
            frames=item["frames"],
            received_at=item["received_at"],
            shutter_changed=item['shutter_changed'],
            follow_target_available=bool(item.get('follow_picker') and fresh and item['state'] == 'live'),
            audio_enabled=bool(self.recognize and self.on_audio),
            recognition='running' if self.recognize and fresh and item['state']=='live' else 'waiting',
            people=item.get('people',0) if fresh else 0,
            hands=item.get('hands',0) if fresh else 0,
            animals=item.get('animals',0) if fresh else 0,
            pet_tracking=dict(item['pet_tracking'],current=False) if item.get('pet_tracking') and not fresh
                         else item.get('pet_tracking'),
            error_class=item.get('error_class'),error_stage=item.get('error_stage'),
            timing=item.get('timing',{}),dropped_frames=item.get('dropped_frames',0),
            room_service=item.get('room_service'),
            capture=item.get('capture',{}),
            camera_control=control,
            manual_control=bool(item.get('control')),
        )

    def frame(self, camera_id):
        item = self.sessions.get(camera_id)
        if not item or self.status(camera_id)["state"] != "live":
            return None
        item["expires"] = time.monotonic() + 60
        return item["frame"]

    def source_current(self, camera_id, process):
        item = self.sessions.get(camera_id)
        return bool(item and item.get('source_active') and item.get('process') is process
                    and process is not None and process.returncode is None
                    and not item.get('privacy_closed') and item['state'] in {'connecting', 'live'})

    def follow_picker(self, camera_id):
        item = self.sessions.get(camera_id)
        if not item or not item.get('follow_picker') or not self.source_current(camera_id, item.get('process')):
            raise ValueError('follow_target_unavailable')
        return item['follow_picker']

    async def control(self, camera_id, *, direction=None, mode=None):
        item = self.sessions.get(camera_id)
        if not item or not item.get('control'):
            raise RuntimeError('camera_not_owned')
        if bool(direction) == bool(mode):
            raise ValueError('invalid_camera_control')
        if mode is not None and mode not in {'auto','pets','hold','survey'}:
            raise ValueError('invalid_camera_mode')
        if mode == 'pets' and not self.recognize:
            raise ValueError('recognition_required')
        if direction is not None and direction not in {'left','right','up','down'}:
            raise ValueError('invalid_motor_direction')
        self._set_mode(camera_id, item, mode or 'hold')
        if direction:
            task = asyncio.current_task()
            item['manual_tasks'].add(task)
            try:return await item['control'](direction,expected_epoch=item['control_epoch'])
            finally:item['manual_tasks'].discard(task)
        return {'mode':item['mode'], 'intent_revision':item['mode_revision']}

    def _set_mode(self, camera_id, item, mode):
        # Manual aiming suspends the automatic director before any network wait.
        # A pending automatic decision must not fight this newer user intent.
        item['control_epoch'] += 1
        item['mode'] = 'hold'
        item['mode_revision'] = secrets.token_hex(16)
        item['survey_attempts'] = 0
        item['survey_confirmed_moves'] = 0
        try:
            intent = self.intent_factory(camera(camera_id)).save(mode)
        except (OSError, ValueError, RuntimeError):
            item['intent_status'] = 'unavailable'
            raise RuntimeError('camera_intent_unavailable') from None
        item.update(mode=intent.mode, mode_revision=intent.revision, intent_status=intent.status)
        item['animals'] = 0
        if item.get('pet_tracking'):item['pet_tracking'] = dict(item['pet_tracking'],current=False)

    async def _monitor(self, selected, item):
        while self.sessions.get(selected['id']) is item:
            process = item.get('process')
            if process is not None and process.returncode is None:
                # 旧采集进程尚未确认退出，继续等待这一进程的退出状态。
                await asyncio.sleep(5)
                continue
            try:
                await self._run(selected, item)
            except Exception as error:
                # 清理异常可能覆盖取消信号。主动停止优先于后台重试。
                if asyncio.current_task().cancelling():
                    raise asyncio.CancelledError from None
                if not self.continuous:
                    raise
                if item['state'] != 'shutter_closed':
                    item['state'] = 'unavailable'
                item.update(error_class=type(error).__name__, error_stage='cleanup')
            if not self.continuous:
                return
            await asyncio.sleep(15 if item["state"] == "shutter_closed" else 5)

    async def _run(self, selected, item):
        process = None
        pipeline = None
        recognizer = None
        lease = None
        io = None
        receiver = None
        motion_task = None
        waiters = {}
        retired = False
        item["privacy_closed"] = False
        mode = item.get('mode')
        item.update(mode=mode if mode in CAMERA_MODES else 'auto' if self.recognize else 'hold',
                    control_epoch=item.get('control_epoch', 0)+1, control=None, manual_tasks=set(),
                    source_active=False, state='connecting')
        latest = None
        stage='initializing'
        wall_epoch = self.wall.begin(selected['id']) if self.wall else None

        def retire_source(reason, error_class=None):
            nonlocal latest, retired
            if retired:
                return
            retired = True
            latest = None
            item.pop('follow_picker', None)
            if item.get('mode') in TARGET_MODES:
                item.update(mode='hold', mode_revision=secrets.token_hex(16))
            previous_state = item['state']
            item.update(source_active=False, frame=None, received_at=0, people=0, hands=0, animals=0,
                        control=None, control_epoch=item['control_epoch']+1)
            if item.get('pet_tracking'):
                item['pet_tracking'] = dict(item['pet_tracking'], current=False)
            if previous_state not in {'shutter_closed', 'stopped'}:
                item['state'] = 'unavailable'
                if error_class or previous_state in {'connecting', 'live'}:
                    item.update(error_class=error_class, error_stage='capture')
            for future in tuple(waiters.values()):
                if not future.done():
                    future.set_exception(RuntimeError(reason))
                    future.exception()
            if self.wall:
                self.wall.clear(selected['id'], wall_epoch)
            # A cancelled owner may finish after start() installed a replacement.
            owner = self.sessions.get(selected['id'])
            if self.on_clear and (owner is None or owner is item):
                self.on_clear(selected['id'])

        try:
            stage='shutter_readback'
            is_open = await camera_is_open(selected)
            if not is_open:
                item['state'] = 'shutter_closed'
                return
            if self.recognize or (self.control_camera and selected.get('takeover') is True):
                stage='camera_lease'
                from .ptz_control import CameraLease, Transport
                io = Transport(selected['id'])
                lease = CameraLease(io, camera_storage(selected['id'])/'ptz-lease.json',
                    open_privacy=False, restore_native=True)
                await lease.acquire()
                item['shutter_changed'] = lease.snapshot['before'].get('camera_power') is False
                if item['shutter_changed']:
                    await asyncio.sleep(5)
            if self.recognize:
                from .camera_recognition import CameraRecognition
                recognizer = CameraRecognition(selected['id'], self.identity_store)
                item['recognizer'] = recognizer
                await recognizer.start()
            elif self.identity_store is not None:
                from homeplane.people.identity_session import IdentitySession
                pipeline = IdentitySession(self.identity_store, selected["id"], secrets.token_hex(12), MODELS)
            capture_session = secrets.token_hex(16)
            audio_ingress = AudioIngress(selected,capture_session) if self.recognize and self.on_audio else None
            process = await asyncio.create_subprocess_exec(
                str(PYTHON),
                "-B",
                "-m",
                "homeplane.embodied.capture_worker",
                cwd=str(PROJECT),
                env=capture_environment(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=6_000_000,
            )
            stage='capture'
            item["process"] = process
            process.stdin.write(
                (
                    json.dumps(dict(source="miloco", camera=selected["id"], motor=lease is not None,
                        audio=bool(audio_ingress), identity=False,capture_session=capture_session)) + "\n"
                ).encode()
            )
            await process.stdin.drain()
            item['source_active'] = True
            queue = asyncio.Queue(maxsize=1)
            last_video_lease=0.0
            async def receive():
                nonlocal last_video_lease
                reason, error_class = 'source_closed', None
                try:
                    while line := await process.stdout.readline():
                        packet = json.loads(line)
                        if not isinstance(packet, dict):
                            raise ValueError('invalid_capture_packet')
                        if packet.get('type') == 'motor_result':
                            future = waiters.get(packet.get('request_id'))
                            if future is not None and not future.done():future.set_result(packet)
                            continue
                        if packet.get('type') == 'audio':
                            if self.on_audio and audio_ingress:
                                admission = (audio_ingress.reject('camera_paused') if item['privacy_closed']
                                    else audio_ingress.accept(packet,wall=time.time(),mono=time.monotonic()))
                                await self.on_audio(selected['id'], admission)
                            continue
                        if packet.get('type')=='video':
                            if self.wall and not item['privacy_closed']:
                                self.wall.offer(selected['id'],wall_epoch,packet)
                            continue
                        if packet.get('type') not in {'frame','error'}:
                            continue
                        if packet['type'] == 'error':
                            if packet.get('reason') == 'privacy_shutter_closed':
                                item.update(state='shutter_closed', privacy_closed=True)
                                reason = 'privacy_shutter_closed'
                            else:
                                reason = 'capture_stream_unavailable'
                            return
                        if packet.get('type')=='frame':
                            if audio_ingress:
                                audio_ingress.observe_frame(packet,wall=time.time(),mono=time.monotonic())
                            if self.wall and time.monotonic()-last_video_lease>.7:
                                last_video_lease=time.monotonic()
                                process.stdin.write((json.dumps(dict(action='video_window',enabled=self.wall.video[selected['id']].subscribers>0))+'\n').encode())
                            item['capture']={k:packet.get(k) for k in ('raw_fps','decoded_fps','hardware_decoding','host_decode_ms','capture_encode_ms')}
                        if self.wall and not item['privacy_closed']:
                            self.wall.offer(selected['id'], wall_epoch, packet)
                        offer_frame(queue, packet)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    reason, error_class = 'capture_stream_unavailable', type(error).__name__
                finally:
                    try:
                        retire_source(reason, error_class)
                    finally:
                        if queue.full():queue.get_nowait()
                        queue.put_nowait({'type':'error', 'reason':reason})
            receiver = asyncio.create_task(receive())
            director = CameraDirector()
            director.apply_mode(item['mode'], item.get('mode_revision', 0))
            director.restore_survey_progress(item.get('survey_attempts', 0), item.get('survey_confirmed_moves', 0))
            motion_lock = asyncio.Lock()
            def privacy_closed():
                item.update(state='shutter_closed',privacy_closed=True)
                retire_source('privacy_shutter_closed')
                if queue.full():queue.get_nowait()
                queue.put_nowait({'type':'error','reason':'privacy_shutter_closed'})
            def current_sample():
                observation = latest if item.get('source_active') else None
                return dict(status='live' if observation else 'connecting',
                    _local_received=observation.get('captured_mono', observation['now']) if observation else -100,
                    session_id=observation['session'] if observation else None,
                    tracking_current=observation.get('tracking_current',False) if observation else False,
                    animals=observation.get('animals',[]) if observation else [],
                    animals_current=observation.get('animals_current',False) if observation else False,
                    animals_intent=observation.get('animals_intent') if observation else None,
                    people=observation['people'] if observation else [],
                    scene_visible=observation.get('scene_visible',False) if observation else False,
                    _follow_jpeg=observation.get('safety_jpeg') if observation else None,
                    _patrol_view=observation.get('_patrol_view') if observation else None)
            def follow_sample():
                sample = current_sample()
                last_motor_sent = item.get('last_motor_sent', -100)
                return dict(sample, _follow_motion=last_motor_sent,
                            _follow_stable=not motion_lock.locked() and sample['_local_received'] > last_motor_sent)
            if lease is not None and self.recognize:
                item['follow_picker'] = CameraFollowPicker(director, follow_sample,
                    lambda: (item['mode'], item['mode_revision']), lambda mode: self._set_mode(selected['id'], item, mode))
            async def send_motion(direction, observed=None, expected_epoch=None):
                if direction not in {'left','right','up','down'}:raise ValueError('invalid_motor_direction')
                if item['privacy_closed']:raise RuntimeError('privacy_shutter_closed')
                if not item.get('source_active'):raise RuntimeError('capture_source_changed')
                if motion_lock.locked():raise RuntimeError('motor_busy')
                began = time.monotonic()
                expires = min(began+1.5, observed+1.5) if observed is not None else began+1.5
                async with motion_lock:
                    request_id = secrets.token_hex(12)
                    correlation = secrets.token_hex(16)
                    future = asyncio.get_running_loop().create_future()
                    waiters[request_id] = future
                    sent = False
                    self.runtime_log.record('embodied.command','start',correlation=correlation)
                    try:
                        if process.returncode is not None:raise RuntimeError('camera_not_owned')
                        if not await camera_is_open(selected):
                            privacy_closed()
                            raise RuntimeError('privacy_shutter_closed')
                        if item['privacy_closed']:raise RuntimeError('privacy_shutter_closed')
                        if not item.get('source_active'):raise RuntimeError('capture_source_changed')
                        if process.returncode is not None:raise RuntimeError('camera_not_owned')
                        if expected_epoch is not None and expected_epoch != item['control_epoch']:
                            raise RuntimeError('camera_intent_changed')
                        now = time.monotonic()
                        current = current_sample()
                        if observed is not None and (not 0 <= now-observed <= 1.5
                                or current['tracking_current'] is not True
                                or current['_local_received'] <= item.get('last_motor_sent', -100)
                                or not director.decision_current(current, now, direction,
                                    focus=self.focus(selected['id']),
                                    occupied_elsewhere=self.occupied_elsewhere(selected['id']),
                                    review_pending=self.review_pending(selected['id'], current.get('session_id')),
                                    fall_pending=self.fall_pending(selected['id'], current.get('session_id')),
                                    sound_cue=self.sound_cue(selected['id'], current.get('session_id'),
                                                           director.sound_search_cue))):
                            raise RuntimeError('stale_input')
                        if now >= expires:raise RuntimeError('motor_request_expired')
                        item['last_motor_sent'] = now
                        if recognizer is not None:recognizer.mark_motion(now)
                        if self.on_clear:self.on_clear(selected['id'])
                        process.stdin.write((json.dumps(dict(action='motor_step',direction=direction,
                            request_id=request_id,expires_mono=expires))+'\n').encode())
                        sent = True
                        await process.stdin.drain()
                        result = await asyncio.wait_for(future,3)
                        if result.get('error_code') == 'motor_request_expired':
                            raise RuntimeError('motor_request_expired')
                        if result.get('error_class') or result.get('native_code')!=0:
                            raise RuntimeError('local_motor_rejected')
                        director.sent(time.monotonic(), direction, automatic=observed is not None)
                        self.runtime_log.record('embodied.command','result',correlation=correlation,
                            elapsed_ms=round((time.monotonic()-began)*1000))
                        return result
                    except asyncio.CancelledError:
                        self.runtime_log.record('embodied.command','result',correlation=correlation,
                            result='cancelled',code='control_cancelled_after_send' if sent else 'control_cancelled_before_dispatch',
                            elapsed_ms=round((time.monotonic()-began)*1000))
                        raise
                    except Exception as error:
                        if str(error) not in {'stale_input', 'camera_intent_changed', 'motor_request_expired',
                                              'privacy_shutter_closed', 'capture_source_changed', 'source_closed',
                                              'capture_stream_unavailable'}:
                            director.failed(time.monotonic())
                        self.runtime_log.record('embodied.command','result',correlation=correlation,
                            result='unavailable',code='runtime_error',elapsed_ms=round((time.monotonic()-began)*1000))
                        raise
                    finally:
                        waiters.pop(request_id,None)
                        if not future.done():future.cancel()
            if lease is not None:item['control'] = send_motion
            async def direct():
                checked = 0.0
                while True:
                    if item['privacy_closed'] or not item['source_active']:return
                    now = time.monotonic()
                    if now-checked >= 10:
                        checked = now
                        if not await camera_is_open(selected):
                            privacy_closed()
                            return
                    if not item['source_active']:return
                    now = time.monotonic()
                    sample = current_sample()
                    epoch = item['control_epoch']
                    director.apply_mode(item['mode'], item.get('mode_revision', 0))
                    direction = director.choose(sample, now, focus=self.focus(selected['id']),
                        occupied_elsewhere=self.occupied_elsewhere(selected['id']),
                        review_pending=self.review_pending(selected['id'], sample.get('session_id')),
                        fall_pending=self.fall_pending(selected['id'], sample.get('session_id')),
                        sound_cue=self.sound_cue(selected['id'], sample.get('session_id'),
                                                director.sound_search_cue)) if item['mode']!='hold' else None
                    item['survey_attempts'] = director.survey_attempts
                    item['survey_confirmed_moves'] = director.survey_confirmed_moves
                    if item['mode']=='hold':director.hold()
                    item['camera_control'] = dict(director.public(),mode=item['mode'],
                                                 intent_status=item.get('intent_status', 'default'))
                    if direction:
                        try:
                            await send_motion(direction,sample['_local_received'],epoch)
                        except (RuntimeError, OSError, TimeoutError):
                            if item['privacy_closed']:return
                    await asyncio.sleep(.3)
            if lease is not None and self.recognize:motion_task=asyncio.create_task(direct())
            checked_power=0.
            while self.continuous or time.monotonic() < item["expires"]:
                if item['privacy_closed'] or not item['source_active']:break
                if motion_task and motion_task.done():
                    await motion_task
                wait_timeout = CAPTURE_RECEIVE_TIMEOUT
                if not self.continuous:
                    wait_timeout = CAPTURE_IDLE_TIMEOUT if not item['frames'] else 40
                packet = await asyncio.wait_for(queue.get(), wait_timeout)
                if not item['source_active']:break
                if time.monotonic()-checked_power>=5:
                    checked_power=time.monotonic()
                    if not await camera_is_open(selected):privacy_closed()
                if item['privacy_closed'] or not item['source_active']:break
                if packet.get("type") == "frame":
                    if recognizer is not None:
                        stage='recognition'
                        recognizer.animals_enabled = item['mode'] in PET_MODES
                        recognizer.animals_intent = item.get('mode_revision')
                        observation = await recognizer.observe(packet,
                            source_current=lambda: self.source_current(selected['id'], process))
                        if item['privacy_closed'] or not item['source_active']:break
                        # Inference finishing after a stale input cannot trigger.
                        item['timing'] = recognizer.timing
                        if observation is None or not 0 <= time.time()-packet['received_at'] <= 1.5:
                            item['dropped_frames'] = item.get('dropped_frames',0)+1
                            if self.on_clear:self.on_clear(selected['id'])
                            continue
                        latest = observation
                        if self.on_observation:
                            await self.on_observation(observation, process)
                        if not self.source_current(selected['id'], process):break
                        item.update(people=len(observation['people']),hands=len(observation['hands']),
                            animals=len(observation.get('animals',[])),pet_tracking=observation.get('pet_tracking'),
                            room_service=observation.get('room_service'))
                        stage='capture'
                    if pipeline is not None:
                        from homeplane.people.live import analyze
                        await analyze(pipeline, base64.b64decode(packet.get("face_jpeg") or packet["jpeg"]))
                    if not self.source_current(selected['id'], process):break
                    if not item["frames"]:
                        item["expires"] = max(item["expires"], time.monotonic() + 60)
                    item.update(
                        state="live",
                        error_class=None,error_stage=None,
                        frame=base64.b64decode(packet.get("preview") or packet["jpeg"]),
                        frames=item["frames"] + 1,
                        received_at=packet["received_at"],
                    )
                elif packet.get("type") == "error":
                    break
            if not item["privacy_closed"]:
                item["state"] = "stopped" if time.monotonic() >= item["expires"] else "unavailable"
        except asyncio.CancelledError:
            item["state"] = "stopped"
            raise
        except Exception as error:
            item.update(state='unavailable',error_class=type(error).__name__,error_stage=stage)
        finally:
            try:
                try:
                    try:
                        retire_source('source_closed')
                    finally:
                        cleanup_tasks = list(item['manual_tasks'])
                        if motion_task:cleanup_tasks.append(motion_task)
                        if receiver:cleanup_tasks.append(receiver)
                        for task in cleanup_tasks:task.cancel()
                        try:
                            await asyncio.gather(*cleanup_tasks, return_exceptions=True)
                        finally:
                            try:
                                if recognizer is not None:await recognizer.close()
                            finally:
                                item['recognizer'] = None
                                try:
                                    if pipeline is not None:await close_identity(pipeline)
                                finally:
                                    item['frame'] = None
                finally:
                    if process and process.returncode is None:

                        try:process.terminate()
                        except ProcessLookupError:pass
                        try:
                            await asyncio.wait_for(process.wait(), 5)
                        except asyncio.TimeoutError:
                            try:process.kill()
                            except ProcessLookupError:pass
                            await process.wait()
            finally:
                try:
                    if lease is not None:
                        item['restored'] = await lease.release()
                        item['camera_control'] = dict(owner='none',phase='restored' if item['restored'] else 'restore_pending',
                            reason='镜头已释放' if item['restored'] else '镜头待释放',view_usable=False,target_locked=False)
                finally:
                    if io is not None:
                        await io.close()

    async def stop(self,camera_id):
        item=self.sessions.pop(camera_id,None)
        if item:
            item['task'].cancel();await asyncio.gather(item['task'],return_exceptions=True)

    async def close(self):
        tasks = [s["task"] for s in self.sessions.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
