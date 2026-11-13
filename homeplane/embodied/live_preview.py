"""管理 Console 实时画面的短期查看权限，不持有图像。"""

import asyncio
import base64
import secrets
import time
from dataclasses import dataclass

import httpx


VIEW_IDLE_SECONDS = 12
MAX_VIEWS = 24


@dataclass
class LiveView:
    token: str
    camera: str
    purpose: str
    source: str
    owner: tuple
    capture: int | None
    manual: bool
    expires: float


class LivePreviews:
    def __init__(self, service, *, clock=time.monotonic):
        self.service = service
        self.clock = clock
        self.views: dict[str, LiveView] = {}
        self.lock = asyncio.Lock()
        self.expiry_task = None
        self.generation = 0
        self.closing_cameras = set()

    def _source(self, manual):
        if manual:
            process = self.service.manual_previews.process
            return (id(process), getattr(process, 'returncode', None))
        return self.service._game_source()

    def _check(self, view):
        if (self.views.get(view.token) is not view or self.clock() >= view.expires
                or view.owner != self._source(view.manual)
                or view.camera in self.closing_cameras
                or self.service.closed or self.service.recovery_pending):
            raise ValueError('camera_preview_ended')
        try:
            source = self.service.preview_source(view.camera)
        except (OSError, ValueError):
            source = None
        if view.source != source:
            raise ValueError('camera_preview_ended')

    def current(self, camera, token, *, purpose=None):
        view = self.views.get(token)
        if view is None or view.camera != camera or purpose is not None and view.purpose != purpose:
            raise ValueError('camera_preview_ended')
        self._check(view)
        return view

    async def open(self, camera, purpose):
        generation = self.generation
        async with self.lock:
            await self._release_manual(self._drop(self._expired()))
            service = self.service
            if (generation != self.generation or camera in self.closing_cameras
                    or service.closed or service.recovery_pending or service.preview_suspended
                    or not service.configured()):
                raise ValueError('camera_preview_unavailable')
            if len(self.views) >= MAX_VIEWS:
                raise ValueError('camera_preview_busy')
            source = service.preview_source(camera)
            before = service._game_source()
            manual = (service.manual_previews.has(camera) or not service.capture_enabled
                      or service.process is None or service.process.returncode is not None)
            if purpose == 'identity' and manual:
                raise ValueError('camera_preview_unavailable')
            if manual:
                report = await service.manual_previews.command('start', camera, source_context=source)
            else:
                report = await service._private_worker_request('/api/previews/' + camera, {}, media_source=source)
            capture = report.pop('capture_epoch', None)
            if not manual and (type(capture) is not int or capture < 0):
                raise ValueError('camera_preview_unavailable')
            token = secrets.token_hex(16)
            view = LiveView(token, camera, purpose, source, self._source(manual), capture,
                            manual, self.clock() + VIEW_IDLE_SECONDS)
            self.views[token] = view
            try:
                if generation != self.generation or before != service._game_source():
                    raise ValueError('camera_preview_ended')
                self._check(view)
            except ValueError:
                await self._release_manual(self._drop([view]))
                raise
            if self.expiry_task is None:
                self.expiry_task = asyncio.create_task(self._expire(), name='friday-live-preview-expiry')
            return dict(report, preview_token=token, recognition_enabled=not manual)

    def _expired(self):
        expired = []
        for view in self.views.values():
            try:
                self._check(view)
            except ValueError:
                expired.append(view)
        return expired

    def _drop(self, views):
        cameras = set()
        for view in views:
            self.views.pop(view.token, None)
            if view.manual:
                cameras.add(view.camera)
        return cameras

    async def _release_manual(self, cameras):
        if not cameras:
            return
        in_use = {view.camera for view in self.views.values() if view.manual}
        manual = self.service.manual_previews
        for camera in cameras - in_use:
            if manual.has(camera):
                await manual.command('stop', camera)
        if not manual.cameras:
            await manual.close()

    async def _expire(self):
        try:
            while self.views:
                await asyncio.sleep(2)
                async with self.lock:
                    try:
                        await self._release_manual(self._drop(self._expired()))
                    except (OSError, ValueError, TimeoutError):
                        # 手动预览进程负责回收失败的采集；权限已经撤销。
                        pass
        finally:
            if self.expiry_task is asyncio.current_task():
                self.expiry_task = None

    async def stop(self, camera, token):
        cameras = self._drop([view for view in self.views.values()
                              if view.camera == camera and token in (None, view.token)])
        if not cameras and token is not None:
            return
        async with self.lock:
            await self._release_manual(cameras)

    async def close(self):
        self.generation += 1
        self.views.clear()
        task, self.expiry_task = self.expiry_task, None
        if task is not None:
            task.cancel()
        async with self.lock:
            await self.service.manual_previews.close()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    async def read(self, camera, token, *, frame=False):
        view = self.current(camera, token)
        service = self.service
        try:
            if view.manual:
                report = await service.manual_previews.command(
                    'frame' if frame else 'status', camera, source_context=view.source)
            else:
                path = '/api/previews/' + camera + ('/frame' if frame else '')
                report = await service._private_worker_request(
                    path, media_source=view.source, media_epoch=view.capture, jpeg=frame)
        except (OSError, httpx.HTTPError, ValueError, TimeoutError) as error:
            if str(error) == 'camera_preview_ended':
                await self.stop(camera, token)
            self.current(camera, token)
            raise
        self.current(camera, token)
        if not frame:
            state = report.get('state')
            if state == 'stopped':
                await self.stop(camera, token)
                raise ValueError('camera_preview_ended')
            if state in {'shutter_closed', 'privacy_closed', 'privacy_shutter_closed'}:
                await self.stop(camera, token)
            else:
                view.expires = self.clock() + VIEW_IDLE_SECONDS
            return dict(report, recognition_enabled=not view.manual)
        if view.manual:
            try:
                image = base64.b64decode(report.get('frame') or '', validate=True)
            except (TypeError, ValueError):
                raise ValueError('camera_frame_unavailable') from None
            if not image or len(image) > 2_000_000:
                raise ValueError('camera_frame_unavailable')
            return image
        return report
