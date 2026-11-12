"""在同一目录来源上生成内存照片副本，持有读取、处理和撤销责任。"""

import asyncio
import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta

from .catalog import FamilyMediaError
from .contracts import FamilyMediaRecipe, FamilyMediaResolveRequest
from .photo_codec import PhotoCodecError
from .photo_contracts import MAX_PHOTO_BYTES, MAX_RENDER_BYTES, MAX_PREVIEW_EDGE, PhotoWorkRequest
from .photo_requests import FamilyMediaPhotoJob, FamilyMediaPhotoOutput
from .photo_runner import PhotoRunner, _join_owned
from .reader import RANGE_BYTES

MAX_JOBS = 8
MAX_HELD_BYTES = 64 * 1024 * 1024
JOB_SECONDS = 60
LEASE_SECONDS = 600


@dataclass
class PhotoLease:
    state: FamilyMediaPhotoJob
    request: object
    fingerprint: str
    kind: str
    deadline: float
    task: asyncio.Task | None = None
    payload: bytes | None = None
    cancel_requested: bool = False
    expiry: asyncio.TimerHandle | None = None


class FamilyPhotos:
    def __init__(self, catalog, *, runner=None):
        self.catalog = catalog
        self.runner = runner or PhotoRunner()
        self.jobs = {}
        self.active = None
        self.closed = False
        self.closing = None

    def _available(self):
        if self.closed or self.catalog.closed:
            raise FamilyMediaError("photo_service_closed")

    def _prune(self):
        now = self.catalog.monotonic()
        for lease in self.jobs.values():
            if lease.deadline <= now and lease.task.done():
                lease.expiry.cancel()
                lease.payload = None
                lease.state = lease.state.model_copy(update={"status": "expired", "output": None, "failure": None})
        # 请求摘要与输出采用同一期限；过期任务由调用者重新打开，不能自动重跑。
        self.jobs = {key: value for key, value in self.jobs.items() if value.deadline > now or not value.task.done()}

    def _expire(self, job_id):
        lease = self.jobs.get(job_id)
        if lease is None:
            return
        lease.payload = None
        lease.state = lease.state.model_copy(update={"status": "expired", "output": None, "failure": None})
        def release(_=None):
            if self.jobs.get(job_id) is lease:
                self.jobs.pop(job_id)
            if self.active is lease:
                self.active = None
        if lease.task.done():
            release()
        else:
            if not lease.cancel_requested:
                lease.cancel_requested = True
                lease.task.cancel()
            # 句柄迟到时仍先回收子进程，不能仅因租约到期就释放处理名额。
            lease.task.add_done_callback(release)

    async def _check_item(self, request, original_hash, original_ref=None):
        self._available()
        result = await self.catalog.resolve_photo(FamilyMediaResolveRequest(
            expected_context=request.expected_context, item_id=request.item_id,
        ))
        item = result.item
        if (result.context_fingerprint != request.expected_context or item.status != "ready"
                or item.media_kind != "image" or item.original_hash != original_hash
                or original_ref is not None and item.original_ref != original_ref):
            raise FamilyMediaError("photo_original_changed_or_not_ready")
        if not 0 < item.size_bytes <= MAX_PHOTO_BYTES:
            raise FamilyMediaError("photo_input_limit")
        self._available()
        return item

    async def _recipe(self, request):
        await self.catalog._check(request.expected_context, online=True)
        row = self.catalog.connection.execute(
            "SELECT body FROM family_media_recipes WHERE recipe_id=?", (request.recipe_id,),
        ).fetchone()
        if row is None:
            raise FamilyMediaError("family_media_recipe_not_found")
        recipe = FamilyMediaRecipe.model_validate_json(row[0])
        if (recipe.context_fingerprint != request.expected_context
                or not recipe.created_at <= self.catalog.clock() < recipe.expires_at):
            raise FamilyMediaError("family_media_recipe_expired_or_context_changed")
        if recipe.status == "rejected" or request.purpose == "export" and recipe.status != "reviewed":
            raise FamilyMediaError("photo_recipe_review_required")
        # 虚拟相册操作有自己的目录语义，不冒充照片像素处理。
        if any(operation.operation == "organize" for operation in recipe.operations):
            raise FamilyMediaError("photo_pixel_recipe_required")
        return recipe

    async def start(self, request, *, kind):
        self._available()
        if kind not in {"open", "render"}:
            raise FamilyMediaError("photo_invalid_request")
        await self.catalog._check(request.expected_context, online=True)
        self._available()
        self._prune()
        fingerprint = hashlib.sha256((kind + request.model_dump_json()).encode()).hexdigest()
        for lease in self.jobs.values():
            if lease.request.request_id == request.request_id:
                if lease.fingerprint != fingerprint:
                    raise FamilyMediaError("photo_request_conflict")
                return lease.state.model_copy(deep=True)
        held = sum(len(lease.payload or b"") for lease in self.jobs.values())
        if self.active is not None:
            raise FamilyMediaError("photo_busy")
        if len(self.jobs) >= MAX_JOBS or held + MAX_RENDER_BYTES > MAX_HELD_BYTES:
            raise FamilyMediaError("photo_capacity")
        created = self.catalog.clock()
        job_id = secrets.token_hex(32)
        state = FamilyMediaPhotoJob(job_id=job_id, status="processing", created_at=created,
            expires_at=created + timedelta(seconds=LEASE_SECONDS), simulated=self.catalog.simulated)
        lease = PhotoLease(state, request.model_copy(deep=True), fingerprint, kind,
            self.catalog.monotonic() + LEASE_SECONDS)
        # 最后一次 await 之后才占用名额，同一事件循环中没有检查后再排队的窗口。
        self.jobs[job_id] = lease
        self.active = lease
        lease.task = asyncio.create_task(self._execute(lease))
        lease.expiry = asyncio.get_running_loop().call_later(LEASE_SECONDS, self._expire, job_id)
        return state.model_copy(deep=True)

    async def _read(self, request, item):
        plan = await self.catalog.prepare_original(FamilyMediaResolveRequest(
            expected_context=request.expected_context, item_id=item.item_id,
        ))
        try:
            if plan.original_hash != item.original_hash or plan.size_bytes != item.size_bytes:
                raise FamilyMediaError("photo_original_changed_or_not_ready")
            source, checksum = bytearray(), hashlib.sha256()
            for offset in range(0, plan.size_bytes, RANGE_BYTES):
                chunk = await self.catalog.read_original(plan.plan_id, offset, min(RANGE_BYTES, plan.size_bytes-offset))
                source.extend(chunk)
                checksum.update(chunk)
            if checksum.hexdigest() != item.original_hash:
                raise FamilyMediaError("photo_original_changed")
            return source
        finally:
            self.catalog.revoke_original(plan.plan_id)

    async def _produce(self, lease):
        request = lease.request
        recipe = await self._recipe(request) if lease.kind == "render" else None
        source_request = FamilyMediaResolveRequest(expected_context=request.expected_context,
            item_id=recipe.item_id if recipe else request.item_id)
        original_hash = recipe.original_hash if recipe else request.expected_original_hash
        item = await self._check_item(source_request, original_hash, recipe.original_ref if recipe else None)
        source = await self._read(source_request, item)
        purpose = request.purpose if recipe else "preview"
        work = PhotoWorkRequest(original_sha256=original_hash, input_bytes=len(source),
            operations=recipe.operations if recipe else [],
            output_format=request.output_format if purpose == "export" else "PNG",
            max_edge=None if purpose == "export" else MAX_PREVIEW_EDGE)
        result, payload = await self.runner.render(source, work)
        del source
        # 解码期间来源、审核或会话可变化；复核以后才公开处理结果。
        await self._check_item(source_request, original_hash, item.original_ref)
        if recipe:
            if (await self._recipe(request)) != recipe:
                raise FamilyMediaError("photo_recipe_changed")
        self._available()
        if lease.cancel_requested or lease.deadline <= self.catalog.monotonic():
            raise FamilyMediaError("photo_expired")
        output = FamilyMediaPhotoOutput(context_fingerprint=request.expected_context,
            item_id=item.item_id, original_ref=item.original_ref, recipe_id=recipe.recipe_id if recipe else None,
            purpose=purpose, result=result, content_url="/api/family-media/photos/content/"+lease.state.job_id,
            created_at=self.catalog.clock(), expires_at=lease.state.expires_at, simulated=self.catalog.simulated)
        lease.payload = payload
        lease.state = lease.state.model_copy(update={"status": "ready", "output": output})

    async def _execute(self, lease):
        try:
            async with asyncio.timeout(JOB_SECONDS):
                await self._produce(lease)
        except asyncio.CancelledError:
            lease.payload = None
            lease.state = lease.state.model_copy(update={"status": "cancelled", "output": None, "failure": None})
        except TimeoutError:
            lease.payload = None
            lease.state = lease.state.model_copy(update={"status": "failed", "output": None, "failure": "photo_timeout"})
        except (FamilyMediaError, PhotoCodecError) as error:
            lease.payload = None
            lease.state = lease.state.model_copy(update={"status": "failed", "output": None, "failure": error.code})
        except Exception:
            # 日志和 HTTP 均不透传解码器、文件名、上游异常或家庭内容。
            lease.payload = None
            lease.state = lease.state.model_copy(update={"status": "failed", "output": None, "failure": "photo_processing_failed"})
        finally:
            if self.active is lease:
                self.active = None

    def _lease(self, job_id):
        self._available()
        self._prune()
        lease = self.jobs.get(job_id)
        if lease is None:
            raise FamilyMediaError("photo_job_not_found")
        return lease

    async def status(self, job_id):
        lease = self._lease(job_id)
        if lease.state.status == "ready":
            await self.content(job_id)
        self._available()
        return lease.state.model_copy(deep=True)

    async def content(self, job_id):
        lease = self._lease(job_id)
        output = lease.state.output
        if output is None or lease.payload is None:
            raise FamilyMediaError("photo_result_not_ready")
        if lease.kind == "render":
            await self._recipe(lease.request)
        request = FamilyMediaResolveRequest(expected_context=output.context_fingerprint, item_id=output.item_id)
        await self._check_item(request, output.result.original_sha256, output.original_ref)
        # 校验等待期间发生撤销、到期或关闭时，不能继续交付已复制的引用。
        self._available()
        if lease.deadline <= self.catalog.monotonic() or lease.state.output is not output or lease.payload is None:
            raise FamilyMediaError("photo_expired")
        return output.model_copy(deep=True), lease.payload

    def owns_result(self, job_id):
        """桌面入口只接纳当前派生副本，路由仍需异步复核来源。"""
        if self.closed or self.catalog.closed:
            return False
        self._prune()
        lease = self.jobs.get(job_id)
        return bool(lease is not None and lease.state.status == "ready" and lease.payload is not None)

    async def cancel(self, job_id):
        lease = self._lease(job_id)
        if not lease.task.done():
            if not lease.cancel_requested:
                lease.cancel_requested = True
                lease.task.cancel()
            try:
                await _join_owned(lease.task)
            except asyncio.CancelledError:
                # 在协程第一次运行之前取消时，任务不会进入其 finally。
                pass
        if self.active is lease and lease.task.done():
            self.active = None
        lease.payload = None
        lease.state = lease.state.model_copy(update={"status": "cancelled", "output": None, "failure": None})
        return lease.state.model_copy(deep=True)

    async def _close(self):
        for lease in self.jobs.values():
            lease.expiry.cancel()
            if not lease.task.done() and not lease.cancel_requested:
                lease.cancel_requested = True
                lease.task.cancel()
        await asyncio.gather(*(lease.task for lease in self.jobs.values()), return_exceptions=True)
        await self.runner.close()
        self.jobs.clear()
        self.active = None

    async def close(self):
        self.closed = True
        if self.closing is None:
            self.closing = asyncio.create_task(self._close())
        await _join_owned(self.closing)
