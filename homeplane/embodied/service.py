"""Optional Friday-owned worker. No model/native SDK imports in the Console."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import socket
import time
from urllib.parse import urlencode

import httpx

from homeplane.runtime_log import observe

from .catalog import get_app
from .paths import DATA, PRIVATE, PROJECT, PYTHON, VISION, camera, configuration


class EmbodiedService:
    def __init__(self):
        self.process = None
        self.lock = asyncio.Lock()
        self.selected = None
        self.open_privacy = False
        self.client = httpx.AsyncClient(timeout=5, trust_env=False, follow_redirects=False)
        self.closed = False
        self.background = False
        self.interaction_enabled = True
        self.health_failures = 0
        self.capture_enabled = True
        self.guardian = None
        self.recovery_pending = False
        self.recovery_enabled = False
        from .manual_preview import ManualPreview
        self.manual_previews=ManualPreview()

    async def ensure_background(self, *, interaction=True, open_privacy=False):
        # Background recovery must never override Mi Home's newer shutter state.
        open_privacy = False
        if not self.capture_enabled:
            await self.stop()
            return
        self.background = True
        self.interaction_enabled = interaction
        if self.process and self.process.returncode is None and (self.open_privacy != open_privacy
                or self.guardian is not None and self.guardian.returncode is not None):
            await self.stop()
        if self.process is None or self.process.returncode is not None:
            await self.start(open_privacy=open_privacy)
        try:
            await self.set_interaction(interaction)
            self.health_failures = 0
        except (httpx.HTTPError, OSError, ValueError):
            self.health_failures += 1
            if self.health_failures >= 3:
                await self.stop()
                self.health_failures = 0
            raise

    async def set_interaction(self, enabled):
        self.interaction_enabled = enabled
        if self.process and self.process.returncode is None:
            await self.request("/api/interaction", {"enabled": enabled})

    async def sync_people(self, people):
        # Names stay in PeopleCatalog; workers only need canonical stable IDs.
        from homeplane.people.live import IdentityStore
        db = IdentityStore(PRIVATE / "identities.sqlite")
        try:
            await asyncio.to_thread(db.sync_people, people)
        finally:
            db.close()

    def configured(self):
        try:
            config = configuration()
            return bool(config.get("enabled") and PYTHON.is_file() and VISION.is_file())
        except (OSError, ValueError, KeyError):
            return False

    def _url(self, path, **query):
        config = configuration()
        key = (PRIVATE / "viewer-key").read_text().strip()
        return f"http://127.0.0.1:{int(config['viewer_port'])}" + path + "?" + urlencode(dict(k=key, **query))

    async def request(self, path, body=None, *, timeout=5):
        response = await self.client.request(
            "GET" if body is None else "POST",
            self._url(path),
            json=body,
            headers={"Origin": f"http://127.0.0.1:{int(configuration()['viewer_port'])}"},
            timeout=timeout,
        )
        if response.status_code >= 400:
            raise ValueError("embodied_operation_unavailable")
        return response.json()

    async def status(self):
        base = dict(
            configured=self.configured(),
            state="not_configured",
            quiet=True,
            camera=self.selected,
            owned=False,
            background=self.background,
            interaction_enabled=self.interaction_enabled,
            capture_enabled=self.capture_enabled,
            recovery_pending=self.recovery_pending,
        )
        if not base["configured"]:
            return base
        base.update(state="stopped" if self.capture_enabled else "paused", quiet=(PRIVATE / "voice-paused").exists(),voice_input=not (PRIVATE/'voice-input-paused').exists(),
            camera_sessions=[dict(camera=c['id'],state='stopped',recognition='waiting') for c in configuration()['cameras']])
        if self.recovery_pending:
            base.update(state='restore_pending', reason='camera_settings_restore_pending')
        if self.process is not None and self.process.returncode is None:
            base.update(owned=True, state="connecting")
            try:
                value = await self.request("/api/status")
                base.update(
                    state=value["status"],
                    reason=value.get("reason"),
                    error_class=value.get("error_class"),
                    error_stage=value.get("error_stage"),
                    camera=value.get("camera_id"),
                    fps=value.get("processed_fps"),
                    latency_ms=value.get("pipeline_ms"),
                    people=len(value.get("people", [])),
                    game=value.get("game"),
                    recording=value.get("recording"),
                    camera_control=value.get("camera_control"),
                    home=value.get("home"),
                    interactions=value.get('interactions'),
                    camera_sessions=value.get('camera_sessions',[]),
                    tv_viewer=value.get('tv_viewer'),
                    voice_input=value.get('voice_input',False),
                    voice_sessions=value.get('voice_sessions',[]),
                )
            except (httpx.HTTPError, OSError, ValueError, KeyError):
                base["state"] = "unavailable"
        return base

    def cameras(self):
        if not self.configured():
            return []
        return [
            dict(
                id=c["id"],
                name=c["name"],
                room=c["room"],
                model=c["model"],
                can_takeover=bool(c.get("takeover")),
                preserve_shutter=not c.get("allow_open"),
                selected=c["id"] == self.selected,
            )
            for c in configuration()["cameras"]
        ]

    @observe("embodied.start")
    async def start(self, camera_id=None, app_id=None, *, open_privacy=False):
        async with self.lock:
            if not self.capture_enabled:
                raise ValueError("camera_capture_paused")
            if open_privacy:
                raise ValueError("camera_open_in_mijia_required")
            if self.closed or not self.configured():
                raise ValueError("embodied_not_configured")
            self.recovery_enabled = True
            # Viewer selection never replaces the household camera owner. Games
            # use its primary feed; other views subscribe to their existing feed.
            selected = camera(self.selected if self.background and self.selected else camera_id)
            if self.process is not None and (
                self.process.returncode is not None or self.selected != selected["id"]
                or open_privacy and not self.open_privacy and not self.background
            ):
                await self._stop()
            if self.process is None:
                await self._recover_pending()
                port = int(configuration()["viewer_port"])
                # Never adopt or terminate a service merely because it has this port.
                with socket.socket() as probe:
                    if probe.connect_ex(("127.0.0.1", port)) == 0:
                        raise ValueError("embodied_port_owned")
                environment = dict(
                    os.environ,
                    FRIDAY_CAMERA_ID=selected["id"],
                    FRIDAY_START_IDLE="1",
                    FRIDAY_CAMERA_OPEN="1" if open_privacy else "0",
                    FRIDAY_CAMERA_HOLD="0" if self.background else "1",
                    FRIDAY_BACKGROUND="1" if self.background else "0",
                    FRIDAY_INTERACTION_ENABLED="1" if self.interaction_enabled else "0",
                    FRIDAY_EMBODIED_DATA=str(DATA),
                    PYTHONPATH=str(PROJECT),
                    PYTHONDONTWRITEBYTECODE="1",
                )
                await self._spawn_guarded_worker(environment)
                self.selected = selected["id"]
                self.open_privacy = open_privacy
                until = time.monotonic() + 12
                while True:
                    if self.process.returncode is not None:
                        raise ValueError("embodied_worker_failed")
                    try:
                        response = await self.request("/health", timeout=0.6)
                        if response.get("ready"):
                            break
                    except (httpx.HTTPError, ValueError):
                        pass
                    if time.monotonic() > until:
                        await self._stop()
                        raise ValueError("embodied_worker_failed")
                    await asyncio.sleep(0.1)
            if app_id:
                app = get_app(app_id)
                if app.mode:
                    await self.request("/api/game", {"action": "select", "mode": app.mode})
            await self.request("/api/start", {})
            return await self.status()

    async def _spawn_guarded_worker(self, environment):
        owner = secrets.token_hex(16)
        read_fd, write_fd = os.pipe()
        guardian = None
        try:
            guardian = await asyncio.create_subprocess_exec(
                str(PYTHON), '-B', '-m', 'homeplane.embodied.camera_guardian',
                '--fd', str(read_fd), '--owner', owner, cwd=str(PROJECT), env=environment,
                pass_fds=(read_fd,), stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
            if await asyncio.wait_for(guardian.stdout.readline(), 5) != b'ready\n':
                raise ValueError('camera_guardian_unavailable')
            self.process = await asyncio.create_subprocess_exec(
                str(PYTHON), '-B', '-m', 'homeplane.embodied.worker', cwd=str(PROJECT),
                env=dict(environment, FRIDAY_CAMERA_OWNER=owner,
                         FRIDAY_CAMERA_LIFETIME_FD=str(write_fd), FRIDAY_SERVICE_OWNER_PID=str(os.getpid())),
                pass_fds=(write_fd,), stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
            self.guardian = guardian
        finally:
            # The worker alone retains the write end. No signal handler/finally
            # in the worker is required to notify its independent guardian.
            os.close(read_fd)
            os.close(write_fd)
            if self.process is None and guardian is not None:
                try:await asyncio.wait_for(guardian.wait(), 6)
                except asyncio.TimeoutError:
                    guardian.kill()
                    await guardian.wait()

    @observe("embodied.stop")
    async def stop(self):
        async with self.lock:
            return await self._stop()

    async def _stop(self):
        # Constructing/closing a Simulator or an unadmitted service never sends
        # device commands. Real household admission also permits orphan recovery.
        if self.process is None and not self.recovery_enabled:
            return dict(status='stopped', restored=True)
        worker_pid = self.process.pid if self.process is not None else None
        if self.process is not None and self.process.returncode is None:
            try:
                await self.request("/api/stop", {}, timeout=50)
            except (httpx.HTTPError, ValueError):
                pass
            try:self.process.terminate()
            except ProcessLookupError:pass
            try:
                await asyncio.wait_for(self.process.wait(), 60)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        await self._recover_pending(worker_pid)
        if self.guardian is not None:
            try:await asyncio.wait_for(self.guardian.wait(), 3)
            except asyncio.TimeoutError:pass  # Durable retry survives Console exit.
            self.guardian = None
        self.process = None
        return dict(status="stopped", restored=True)

    async def _recover_pending(self, worker_pid=None):
        from .ptz_control import read_lease
        try:cameras = configuration()['cameras']
        except FileNotFoundError:return
        except (ValueError, OSError, KeyError):
            self.recovery_pending = True
            raise ValueError('embodied_restore_pending') from None
        async def recover(selected):
            lease = PRIVATE / "cameras" / selected / "ptz-lease.json"
            try:record = read_lease(lease) if lease.exists() else {}
            except (OSError, ValueError):return False
            if record and record.get('status') != 'restored' and (worker_pid is None or record.get('pid') == worker_pid):
                environment = dict(
                    os.environ,
                    FRIDAY_CAMERA_ID=selected,
                    FRIDAY_EMBODIED_DATA=str(DATA),
                    PYTHONPATH=str(PROJECT),
                )
                repair = await asyncio.create_subprocess_exec(
                    str(PYTHON),
                    "-B",
                    "-m",
                    "homeplane.embodied.ptz_control",
                    env=environment,
                    cwd=str(PROJECT),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                try:
                    await asyncio.wait_for(repair.wait(), 55)
                except asyncio.TimeoutError:
                    repair.kill()
                    await repair.wait()
                try:return read_lease(lease).get('status') == 'restored'
                except (ValueError, OSError):return False
            return True
        results = await asyncio.gather(*(recover(c['id']) for c in cameras), return_exceptions=True)
        self.recovery_pending = any(result is not True for result in results)
        if self.recovery_pending:raise ValueError('embodied_restore_pending')

    async def quiet(self, enabled):
        marker = PRIVATE / "voice-paused"
        if enabled:
            marker.touch(mode=0o600)
        else:
            marker.unlink(missing_ok=True)
        if self.process and self.process.returncode is None:
            await self.request("/api/voice", dict(quiet=enabled))
        return dict(quiet=enabled)

    async def voice_input(self, enabled):
        marker=PRIVATE/'voice-input-paused'
        if enabled:marker.unlink(missing_ok=True)
        else:marker.touch(mode=0o600)
        if self.process and self.process.returncode is None:
            await self.request('/api/voice',{'input':enabled},timeout=15)
        return await self.status()

    @observe("embodied.preview")
    async def preview(self, camera_id, *, start=False, frame=False,explicit=False):
        manual_active=self.manual_previews.has(camera_id)
        if not self.capture_enabled and not manual_active and not (explicit and start):
            raise ValueError('camera_capture_paused')
        camera(camera_id)
        if manual_active or explicit and start and (not self.capture_enabled or not self.process or self.process.returncode is not None):
            if self.closed or not self.configured():raise ValueError('embodied_not_configured')
            report=await self.manual_previews.command('start' if start else 'frame' if frame else 'status',camera_id)
            if frame:
                value=report.get('frame')
                if not value:raise ValueError('camera_frame_unavailable')
                return base64.b64decode(value,validate=True)
            return dict(report,recognition_enabled=False)
        if not self.capture_enabled:
            raise ValueError("camera_capture_paused")
        if not self.process or self.process.returncode is not None:
            raise ValueError("embodied_not_running")
        if frame:
            result = await self.client.get(self._url("/api/previews/" + camera_id + "/frame"))
            if result.status_code != 200 or len(result.content) > 2_000_000:
                raise ValueError("camera_frame_unavailable")
            return result.content
        return await self.request("/api/previews/" + camera_id, {} if start else None)

    async def stop_preview(self,camera_id,token=None):
        camera(camera_id)
        if self.manual_previews.has(camera_id):
            if token is not None and self.manual_previews.tokens.get(camera_id)!=token:return dict(state='superseded')
            await self.manual_previews.command('stop',camera_id)
            if not self.manual_previews.cameras:await self.manual_previews.close()
        return dict(state='stopped')

    async def camera_control(self,camera_id,*,direction=None,mode=None):
        camera(camera_id)
        if self.manual_previews.has(camera_id):
            if mode=='auto' or mode=='survey':raise ValueError('recognition_required')
            return await self.manual_previews.command('control',camera_id,direction=direction,mode=mode)
        if not self.capture_enabled or not self.process or self.process.returncode is not None:
            raise ValueError('camera_not_running')
        return await self.request(f'/api/previews/{camera_id}/control',dict(direction=direction) if direction else dict(mode=mode))

    async def camera_power(self,camera_id,*,enabled=None,revision=None,event_id=None):
        camera(camera_id)
        if self.closed or not self.configured():raise ValueError('embodied_not_configured')
        if enabled is False:await self.stop_preview(camera_id)
        request=dict(camera=camera_id,action='read' if enabled is None else 'set')
        if enabled is not None:request.update(enabled=enabled,revision=revision,event_id=event_id)
        process=await asyncio.create_subprocess_exec(str(PYTHON),'-B','-m','homeplane.embodied.camera_power',
            cwd=str(PROJECT),env=dict(os.environ,PYTHONPATH=str(PROJECT),FRIDAY_EMBODIED_DATA=str(DATA)),
            stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
        try:
            output,_=await asyncio.wait_for(process.communicate(json.dumps(request).encode()),38)
            if len(output)>8192:raise ValueError('camera_power_unavailable')
            result=json.loads(output)
            if result.get('status')=='unavailable':raise ValueError('camera_power_unavailable')
            return result
        finally:
            if process.returncode is None:process.kill();await process.wait()

    async def connection(self,action='status',return_url=None):
        if self.closed or not PYTHON.is_file() or not (PRIVATE/'miot-session.json').is_file():
            return dict(state='unconfigured',authorization='missing',cameras=[])
        payload=dict(action=action)
        if action=='finish':payload['return_url']=return_url
        process=await asyncio.create_subprocess_exec(str(PYTHON),'-B','-m','homeplane.embodied.miot_session',
            cwd=str(PROJECT),env=dict(os.environ,PYTHONPATH=str(PROJECT),FRIDAY_EMBODIED_DATA=str(DATA)),
            stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
        try:
            output,_=await asyncio.wait_for(process.communicate(json.dumps(payload).encode()),55)
            if len(output)>8192:raise ValueError('mijia_connection_unavailable')
            return json.loads(output)
        finally:
            if process.returncode is None:process.kill();await process.wait()

    async def scenes(self,*,scene=None,revision=None,event_id=None):
        if self.closed or not PYTHON.is_file() or not (PRIVATE/'miot-session.json').is_file():
            raise ValueError('mijia_scenes_unavailable')
        payload=dict(action='list' if scene is None else 'run')
        if scene is not None:payload.update(scene=scene,revision=revision,event_id=event_id)
        process=await asyncio.create_subprocess_exec(str(PYTHON),'-B','-m','homeplane.embodied.scenes',
            cwd=str(PROJECT),env=dict(os.environ,PYTHONPATH=str(PROJECT),FRIDAY_EMBODIED_DATA=str(DATA)),
            stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
        try:
            output,_=await asyncio.wait_for(process.communicate(json.dumps(payload).encode()),50)
            if len(output)>128000:raise ValueError('mijia_scenes_unavailable')
            result=json.loads(output)
            if result.get('connected') is False:raise ValueError('mijia_scenes_unavailable')
            return result
        finally:
            if process.returncode is None:process.kill();await process.wait()

    async def _identity_request(self, path, body=None):
        if not self.process or self.process.returncode is not None:
            raise ValueError("identity_not_running")
        try:
            response = await self.client.request(
                "GET" if body is None else "POST", self._url(path), json=body,
                headers={"Origin": f"http://127.0.0.1:{int(configuration()['viewer_port'])}",
                         "X-Friday-Worker": (PRIVATE / "control-key").read_text().strip()}, timeout=8)
            if response.status_code != 200:
                raise ValueError(response.json().get("detail", "identity_enrollment_unavailable"))
            return response.json()
        except httpx.HTTPError:
            raise ValueError("identity_worker_unavailable") from None

    async def identity_status(self, camera_id=None):
        report = await self._identity_request("/api/internal/identity")
        if camera_id:
            camera(camera_id)
            return next((r for r in report.get('sessions', [report]) if r.get('camera') == camera_id),
                        dict(camera=camera_id, state='waiting', tracks=[]))
        return report

    async def identity_enroll(self, camera_id, session, track, person):
        return await self._identity_request("/api/internal/identity/register",
                                           dict(camera=camera_id, session=session, track=track, person=person))

    async def miot_cover(self, action, event_id, expected_revision=None):
        # Curtain control does not require camera capture or recognition.
        import time
        from .miot_spec import optional_cover,cover_revision
        binding=optional_cover()
        if binding is None:raise ValueError('cover_unavailable')
        revision=cover_revision(binding)
        if action!='read' and expected_revision!=revision:raise ValueError('cover_binding_changed')
        cache=getattr(self,'_cover_cache',None)
        if action=='read' and cache and cache[0]==revision and time.monotonic()-cache[1]<15:return cache[2]
        if action!='read':self._cover_cache=None
        process=await asyncio.create_subprocess_exec(str(PYTHON),'-B','-m','homeplane.embodied.cover_worker',
            cwd=str(PROJECT),env=dict(os.environ,PYTHONPATH=str(PROJECT),FRIDAY_EMBODIED_DATA=str(DATA)),
            stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
        try:
            output,_=await asyncio.wait_for(process.communicate(json.dumps(dict(action=action,event_id=event_id,expected_revision=expected_revision)).encode()),30)
            if len(output)>8192:raise ValueError('curtain_output_limit')
            result=json.loads(output)
            if action=='read':self._cover_cache=(revision,time.monotonic(),result)
            return result
        finally:
            if process.returncode is None:
                process.kill();await process.wait()

    def viewer(self, app_id, *, tv=False):
        app = get_app(app_id)
        config = configuration()
        host = config["viewer_host"] if tv else "127.0.0.1"
        path = "/games" if app.mode else "/"
        return f"http://{host}:{int(config['viewer_port'])}{path}?" + urlencode(
            dict(k=(PRIVATE / "viewer-key").read_text().strip(), **({} if tv else {'embed':'1'}))
        )

    async def close(self):
        self.closed = True
        try:
            await self.manual_previews.close()
            if getattr(self, 'safety_output', None) is not None:
                await self.safety_output.close()
            await self.stop()
        finally:
            await self.client.aclose()
