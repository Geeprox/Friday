"""Owned camera sessions: bounded previews, identity and whole-home recognition."""

import asyncio
import base64
import json
import os
import secrets
import time

from .paths import DATA, MODELS, PROJECT, PYTHON, camera, camera_storage, capture_environment
from .camera_director import CameraDirector
from .frame_sampling import offer_frame
from homeplane.runtime_log import RuntimeLog


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
    login = json.loads((PRIVATE / "miot-session.json").read_text())
    api = client_factory(cloud_server=selected.get('region', 'cn'), access_token=login["access_token"])
    try:
        rows = await asyncio.wait_for(api.get_props_async(
            [parameter_factory(did=selected["device"]["did"], siid=siid, piid=piid)]), 12)
    finally:
        await api.deinit_async()
    if not rows or rows[0].get("code") != 0 or type(rows[0].get("value")) is not bool:
        raise RuntimeError("camera_state_unavailable")
    return rows[0]["value"]


class PreviewPool:
    def __init__(self, identity_store=None, continuous=False, *, recognize=False, on_observation=None, on_audio=None, on_clear=None, focus=None, occupied_elsewhere=None, wall=None):
        self.wall = wall
        self.sessions = {}
        self.focus = focus or (lambda camera: None)
        self.occupied_elsewhere = occupied_elsewhere or (lambda camera: False)
        self.runtime_log = RuntimeLog(DATA / "logs" / "camera-motion")
        self.lock = asyncio.Lock()
        self.identity_store = identity_store
        self.continuous = continuous
        self.recognize = recognize
        self.on_observation, self.on_audio, self.on_clear = on_observation, on_audio, on_clear

    async def start(self, camera_id):
        selected = camera(camera_id)
        async with self.lock:
            old = self.sessions.get(camera_id)
            if old and not old["task"].done():
                old["expires"] = time.monotonic() + 60
                return self.status(camera_id)
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
        if control and (not fresh or item['state']!='live'):
            control = dict(control,view_usable=False,target_locked=False)
        return dict(
            state=item["state"] if fresh or item["state"] != "live" else "stale",
            camera=camera_id,
            frames=item["frames"],
            received_at=item["received_at"],
            shutter_changed=item['shutter_changed'],
            audio_enabled=self.recognize,
            recognition='running' if self.recognize and fresh and item['state']=='live' else 'waiting',
            people=item.get('people',0) if fresh else 0,
            hands=item.get('hands',0) if fresh else 0,
            error_class=item.get('error_class'),error_stage=item.get('error_stage'),
            timing=item.get('timing',{}),dropped_frames=item.get('dropped_frames',0),
            capture=item.get('capture',{}),
            camera_control=control,
        )

    def frame(self, camera_id):
        item = self.sessions.get(camera_id)
        if not item or self.status(camera_id)["state"] != "live":
            return None
        item["expires"] = time.monotonic() + 60
        return item["frame"]

    async def control(self, camera_id, *, direction=None, mode=None):
        item = self.sessions.get(camera_id)
        if not item or not item.get('control'):
            raise RuntimeError('camera_not_owned')
        if bool(direction) == bool(mode):
            raise ValueError('invalid_camera_control')
        if mode is not None and mode not in {'auto','hold','survey'}:
            raise ValueError('invalid_camera_mode')
        if direction is not None and direction not in {'left','right','up','down'}:
            raise ValueError('invalid_motor_direction')
        # Manual aiming suspends the automatic director before any network wait.
        # A pending automatic decision must not fight this newer user intent.
        item['control_epoch'] += 1
        item['mode'] = mode or 'hold'
        if direction:
            task = asyncio.current_task()
            item['manual_tasks'].add(task)
            try:return await item['control'](direction,expected_epoch=item['control_epoch'])
            finally:item['manual_tasks'].discard(task)
        return {'mode':item['mode']}

    async def _monitor(self, selected, item):
        while True:
            await self._run(selected, item)
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
        item["privacy_closed"] = False
        item.update(mode='auto', control_epoch=0, control=None, manual_tasks=set())
        latest = None
        stage='initializing'
        wall_epoch = self.wall.begin(selected['id']) if self.wall else None
        try:
            stage='shutter_readback'
            is_open = await camera_is_open(selected)
            if not is_open and os.environ.get('FRIDAY_CAMERA_OPEN') != '1':
                item['state'] = 'shutter_closed'
                return
            if self.recognize:
                stage='camera_lease'
                from .ptz_control import CameraLease, Transport
                io = Transport(selected['id'])
                lease = CameraLease(io, camera_storage(selected['id'])/'ptz-lease.json',
                    open_privacy=os.environ.get('FRIDAY_CAMERA_OPEN')=='1', restore_native=False)
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
                from homeplane.people.live import FaceIdentityPipeline
                pipeline = FaceIdentityPipeline(self.identity_store, selected["id"], secrets.token_hex(12), MODELS)
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
                    json.dumps(dict(source="miloco", camera=selected["id"], motor=lease is not None, audio=self.recognize, identity=False)) + "\n"
                ).encode()
            )
            await process.stdin.drain()
            queue = asyncio.Queue(maxsize=1)
            last_video_lease=0.0
            async def receive():
                nonlocal last_video_lease
                try:
                    while line := await process.stdout.readline():
                        packet = json.loads(line)
                        if packet.get('type') == 'motor_result':
                            future = waiters.get(packet.get('request_id'))
                            if future is not None and not future.done():future.set_result(packet)
                            continue
                        if packet.get('type') == 'audio':
                            if self.on_audio:
                                await self.on_audio(selected['id'], packet)
                            continue
                        if packet.get('type')=='video':
                            if self.wall and not item['privacy_closed']:
                                self.wall.offer(selected['id'],wall_epoch,packet)
                            continue
                        if packet.get('type') not in {'frame','error'}:
                            continue
                        if packet.get('type')=='frame':
                            if self.wall and time.monotonic()-last_video_lease>.7:
                                last_video_lease=time.monotonic()
                                process.stdin.write((json.dumps(dict(action='video_window',enabled=self.wall.video[selected['id']].subscribers>0))+'\n').encode())
                            item['capture']={k:packet.get(k) for k in ('raw_fps','decoded_fps','hardware_decoding','host_decode_ms','capture_encode_ms')}
                        if self.wall and not item['privacy_closed']:
                            self.wall.offer(selected['id'], wall_epoch, packet)
                        offer_frame(queue, packet)
                finally:
                    if queue.full():queue.get_nowait()
                    queue.put_nowait({'type':'error'})
            receiver = asyncio.create_task(receive())
            director = CameraDirector()
            motion_lock = asyncio.Lock()
            def privacy_closed():
                item.update(state='shutter_closed',privacy_closed=True)
                if self.wall:self.wall.clear(selected['id'], wall_epoch)
                if queue.full():queue.get_nowait()
                queue.put_nowait({'type':'error','reason':'privacy_shutter_closed'})
            async def send_motion(direction, observed=None, expected_epoch=None):
                if item['privacy_closed']:raise RuntimeError('privacy_shutter_closed')
                if motion_lock.locked():raise RuntimeError('motor_busy')
                async with motion_lock:
                    if process.returncode is not None:raise RuntimeError('camera_not_owned')
                    if not await camera_is_open(selected):
                        privacy_closed()
                        raise RuntimeError('privacy_shutter_closed')
                    if item['privacy_closed']:raise RuntimeError('privacy_shutter_closed')
                    if expected_epoch is not None and expected_epoch != item['control_epoch']:
                        raise RuntimeError('camera_intent_changed')
                    if observed is not None and not 0 <= time.monotonic()-observed <= 1.5:
                        raise RuntimeError('stale_input')
                    request_id = secrets.token_hex(12)
                    correlation = secrets.token_hex(16)
                    began = time.monotonic()
                    future = asyncio.get_running_loop().create_future()
                    waiters[request_id] = future
                    self.runtime_log.record('embodied.command','start',correlation=correlation)
                    try:
                        recognizer.mark_motion(time.monotonic())
                        if self.on_clear:self.on_clear(selected['id'])
                        process.stdin.write((json.dumps(dict(action='motor_step',direction=direction,
                            request_id=request_id))+'\n').encode())
                        await process.stdin.drain()
                        result = await asyncio.wait_for(future,3)
                        if result.get('error_class') or result.get('native_code')!=0:
                            raise RuntimeError('local_motor_rejected')
                        director.sent(time.monotonic(), direction)
                        self.runtime_log.record('embodied.command','result',correlation=correlation,
                            elapsed_ms=round((time.monotonic()-began)*1000))
                        return result
                    except asyncio.CancelledError:
                        self.runtime_log.record('embodied.command','result',correlation=correlation,
                            result='cancelled',code='control_cancelled_after_send',elapsed_ms=round((time.monotonic()-began)*1000))
                        raise
                    except Exception:
                        director.failed(time.monotonic())
                        self.runtime_log.record('embodied.command','result',correlation=correlation,
                            result='unavailable',code='runtime_error',elapsed_ms=round((time.monotonic()-began)*1000))
                        raise
                    finally:waiters.pop(request_id,None)
            if lease is not None:item['control'] = send_motion
            async def direct():
                checked = 0.0
                while True:
                    if item['privacy_closed']:return
                    now = time.monotonic()
                    if now-checked >= 10:
                        checked = now
                        if not await camera_is_open(selected):
                            privacy_closed()
                            return
                    now = time.monotonic()
                    observation = latest
                    sample = dict(status='live' if observation else 'connecting',
                        _local_received=observation.get('captured_mono', observation['now']) if observation else -100,
                        session_id=observation['session'] if observation else None,
                        people=observation['people'] if observation else [],
                        scene_visible=observation.get('scene_visible',False) if observation else False)
                    epoch = item['control_epoch']
                    direction = director.choose(sample, now, focus=self.focus(selected['id']),
                        occupied_elsewhere=self.occupied_elsewhere(selected['id'])) if item['mode']!='hold' else None
                    if item['mode']=='hold':director.hold()
                    item['camera_control'] = dict(director.public(),mode=item['mode'])
                    if direction:
                        try:
                            await send_motion(direction,sample['_local_received'],epoch)
                        except (RuntimeError, OSError, TimeoutError):
                            if item['privacy_closed']:return
                    await asyncio.sleep(.3)
            if lease is not None:motion_task=asyncio.create_task(direct())
            while self.continuous or time.monotonic() < item["expires"]:
                if item['privacy_closed']:break
                if motion_task and motion_task.done():
                    await motion_task
                packet = await asyncio.wait_for(queue.get(), 75 if not item["frames"] else 40)
                if item['privacy_closed']:break
                if packet.get("type") == "frame":
                    if recognizer is not None:
                        stage='recognition'
                        observation = await recognizer.observe(packet)
                        if item['privacy_closed']:break
                        # Inference finishing after a stale input cannot trigger.
                        item['timing'] = recognizer.timing
                        if observation is None or not 0 <= time.time()-packet['received_at'] <= 1.5:
                            item['dropped_frames'] = item.get('dropped_frames',0)+1
                            if self.on_clear:self.on_clear(selected['id'])
                            continue
                        latest = observation
                        if self.on_observation:
                            await self.on_observation(observation, process)
                        item.update(people=len(observation['people']),hands=len(observation['hands']))
                        stage='capture'
                    if pipeline is not None:
                        from homeplane.people.live import analyze
                        await analyze(pipeline, base64.b64decode(packet.get("face_jpeg") or packet["jpeg"]))
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
            if self.wall:self.wall.clear(selected['id'], wall_epoch)
            item['control'] = None
            item['control_epoch'] += 1
            for task in item['manual_tasks']:task.cancel()
            await asyncio.gather(*item['manual_tasks'],return_exceptions=True)
            if self.on_clear:self.on_clear(selected['id'])
            if motion_task:
                motion_task.cancel()
                await asyncio.gather(motion_task,return_exceptions=True)
            if receiver:
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)
            if recognizer is not None:
                await recognizer.close()
                item['recognizer'] = None
            if pipeline is not None:
                pipeline.close()
            item["frame"] = None
            if process and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            if lease is not None:
                item['restored'] = await lease.release()
                item['camera_control'] = dict(owner='none',phase='restored' if item['restored'] else 'restore_pending',
                    reason='镜头已释放' if item['restored'] else '镜头待释放',view_usable=False,target_locked=False)
            if io is not None:
                await io.close()

    async def close(self):
        tasks = [s["task"] for s in self.sessions.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
