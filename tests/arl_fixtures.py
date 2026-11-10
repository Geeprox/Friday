"""合成测试的 ARL 生命周期与显式完整宿主输入边界。"""

import importlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from homeplane.arl import ARLRuntime


def require_priority_host_sdk():
    """完整宿主集成单独选择来源；不在已导入命名空间中混换 SDK。"""
    source = os.environ.get("FRIDAY_TEST_ARL_PRIORITY_HOST_SOURCE")
    if not source:
        pytest.skip("需显式完整宿主 SDK；公开冻结客户端不包含私有宿主组合", allow_module_level=True)
    source = Path(source).resolve()
    arl = importlib.import_module("arl")
    if Path(arl.__file__).resolve() != source / "arl/__init__.py":
        pytest.fail("完整宿主集成需在新进程中使用所选 SDK；禁止混用已导入的冻结客户端")
    for name in ["friday_profile.py", "priority_host.py"]:
        if not (source / "arl" / name).is_file():
            pytest.fail("显式宿主输入缺少所需接口：" + name)
    return source


def isolated_runtime(directory):
    """保留真实 Friday 生命周期，仅替换外部 SDK 会话与元数据传输。"""
    counts = dict(created=0, started=0, closed=0)

    class Session:
        def __init__(self, home):
            counts["created"] += 1
            self.client = SimpleNamespace(home=home)
            self.connected, self.error, self.accepted = False, None, []

        def start(self):
            counts["started"] += 1

        def close(self):
            counts["closed"] += 1

    class Metadata:
        def rpc(self, method):
            assert method == "overview", "本夹具只允许合成元数据读取"
            return dict(
                schema_version=1,
                protocol="arl/1",
                version="synthetic",
                health=dict(live=False, ready=False),
                policy=dict(local_inference_enabled=False, identity_verifier_configured=False),
                providers=[],
                capabilities=[],
            )

    def no_task_client(*_args, **_kwargs):
        raise AssertionError("本夹具不允许外部任务或 SDK 客户端")

    runtime = ARLRuntime(
        lambda: Path(directory),
        state_directory=lambda: Path(directory),
        application_factory=Session,
        client_factory=no_task_client,
        settings_client_factory=lambda _home: Metadata(),
    )
    return runtime, counts
