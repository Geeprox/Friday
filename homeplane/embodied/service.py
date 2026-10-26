"""Optional Friday-owned worker. No model/native SDK imports in the Console."""

from __future__ import annotations

import asyncio
import json
import os
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

    async def ensure_background(self, *, interaction=True, open_privacy=False):
        self.background = True
        self.interaction_enabled = interaction
        if self.process and self.process.returncode is None and self.open_privacy != open_privacy:
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
        )
        if not base["configured"]:
            return base
        base.update(state="stopped", quiet=(PRIVATE / "voice-paused").exists(),voice_input=not (PRIVATE/'voice-input-paused').exists(),
            camera_sessions=[dict(camera=c['id'],state='stopped',recognition='waiting') for c in configuration()['cameras']])
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
            if self.closed or not self.configured():
                raise ValueError("embodied_not_configured")
            # Viewer selection never replaces the household camera owner. Games
            # use its primary feed; other views subscribe to their existing feed.
            selected = camera(self.selected if self.background and self.selected else camera_id)
            if self.process is not None and (
                self.process.returncode is not None or self.selected != selected["id"]
                or open_privacy and not self.open_privacy and not self.background
            ):
                await self._stop()
            if self.process is None:
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
                self.process = await asyncio.create_subprocess_exec(
                    str(PYTHON),
                    "-B",
                    "-m",
                    "homeplane.embodied.worker",
                    cwd=str(PROJECT),
                    env=environment,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    start_new_session=True,
                )
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

    @observe("embodied.stop")
    async def stop(self):
        async with self.lock:
            return await self._stop()

    async def _stop(self):
        if self.process is None:
            return dict(status="stopped", restored=True)
        if self.process.returncode is None:
            try:
                await self.request("/api/stop", {}, timeout=50)
            except (httpx.HTTPError, ValueError):
                pass
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 60)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        # Recover a crashed worker's durable lease using the official transport.
        restore_pending=False
        worker_pid=self.process.pid
        for selected in [c['id'] for c in configuration()['cameras']]:
            lease = PRIVATE / "cameras" / selected / "ptz-lease.json"
            record=json.loads(lease.read_text()) if lease.exists() else {}
            if record.get('pid')==worker_pid and record.get("status") != "restored":
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
                if json.loads(lease.read_text()).get("status") != "restored":
                    restore_pending=True
        if restore_pending:raise ValueError('embodied_restore_pending')
        self.process = None
        return dict(status="stopped", restored=True)

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
    async def preview(self, camera_id, *, start=False, frame=False):
        camera(camera_id)
        if not self.process or self.process.returncode is not None:
            raise ValueError("embodied_not_running")
        if frame:
            result = await self.client.get(self._url("/api/previews/" + camera_id + "/frame"))
            if result.status_code != 200 or len(result.content) > 2_000_000:
                raise ValueError("camera_frame_unavailable")
            return result.content
        return await self.request("/api/previews/" + camera_id, {} if start else None)

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

    async def miot_cover(self, action, event_id):
        # Curtain control does not require camera capture or recognition.
        process=await asyncio.create_subprocess_exec(str(PYTHON),'-B','-m','homeplane.embodied.cover_worker',
            cwd=str(PROJECT),env=dict(os.environ,PYTHONPATH=str(PROJECT),FRIDAY_EMBODIED_DATA=str(DATA)),
            stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
        try:
            output,_=await asyncio.wait_for(process.communicate(json.dumps(dict(action=action,event_id=event_id)).encode()),30)
            if len(output)>8192:raise ValueError('curtain_output_limit')
            return json.loads(output)
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
            if getattr(self, 'safety_output', None) is not None:
                await self.safety_output.close()
            await self.stop()
        finally:
            await self.client.aclose()
