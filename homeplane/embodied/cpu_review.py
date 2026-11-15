"""CPU 视觉复核采用独立离线进程，沿用窗口时效与撤销边界。"""

import hashlib
import json
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from typing import Literal
from .paths import DATA, MODELS, PRIVATE
from .temporal_model import GIB, HostBudget, MlxWindowProcess, VisualResult, host_budget

MODEL_ID = 'Qwen/Qwen3.5-2B'
REVISION = '15852e8c16360a2fea060d615a32b45270f8a8fc'
MODEL = MODELS/'qwen3.5-2b-cpu'
PYTHON = DATA/'review-runtime/bin/python'
MEMORY_LIMIT = 16*GIB
MANIFEST = Path(__file__).with_name('cpu-review-model.json')


def verify(model, *, hashes=False):
    """就绪检查只看固定清单；进程加载前再逐文件校验摘要。"""
    model = Path(model)
    if model.is_symlink():
        raise ValueError('model_pin_mismatch')
    for name, pin in json.loads(MANIFEST.read_text()).items():
        path = model/name
        if path.is_symlink() or not path.is_file() or path.stat().st_size != pin['size']:
            raise ValueError('model_pin_mismatch' if path.exists() else 'model_not_installed')
        if hashes:
            with path.open('rb') as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != pin['sha256']:
                    raise ValueError('model_pin_mismatch')


def readiness(model=MODEL):
    reason = None
    try:
        verify(model)
        if not PYTHON.is_file():
            reason = 'local_runtime_missing'
        elif not sys.platform.startswith('linux'):
            reason = 'linux_required'
    except (OSError, ValueError) as error:
        reason = str(error) if str(error) in {'model_pin_mismatch', 'model_not_installed'} else 'model_not_installed'
    return dict(available=reason is None, reason=reason, model=MODEL_ID, backend='transformers_cpu',
                mode='bounded_window_review', native_streaming=False, max_frames=4,
                max_image_edge=256, parallel_requests=1, min_request_interval_seconds=6,
                sample_interval_seconds=1, idle_exit_seconds=60, memory_limit_mib=16384,
                minimum_host_memory_gib=24, request_timeout_seconds=12,
                performance_verified=False, automatic_announcement=False, cloud_fallback=False)


class CpuReply(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    version: Literal[1]
    result: VisualResult
    peak_rss_bytes: int = Field(ge=0, le=MEMORY_LIMIT)


class CpuBudget(HostBudget):
    def reason(self, *, resident=False):
        if self.total_bytes < 24*GIB:
            return 'memory_capacity'
        if self.free_percent is None:
            return 'memory_unknown'
        if self.total_bytes*self.free_percent/100 < (3 if resident else 16)*GIB:
            return 'memory_pressure'
        return super().reason(resident=resident)


def cpu_budget():
    value = host_budget()
    return CpuBudget(value.total_bytes, value.free_percent, value.load, value.cores)


class CpuWindowProcess(MlxWindowProcess):
    startup_timeout = 120

    def __init__(self, model=MODEL, private=PRIVATE, *, budget=cpu_budget):
        super().__init__(model, private, budget=budget)
        self.peak_rss_bytes = 0

    def readiness(self):
        return readiness(self.model)

    def command(self):
        return (str(PYTHON), '-u', '-m', 'homeplane.embodied.cpu_review_worker', '--model', str(self.model))

    def parse_reply(self, data):
        reply = CpuReply.model_validate_json(data)
        self.peak_rss_bytes = reply.peak_rss_bytes
        return reply.result
