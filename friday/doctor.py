"""Read-only readiness report. Never activates a camera or writes a device."""

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx

from .config import load_settings
from .home import Credentials, HAHome
from .providers import runtime_for


async def main():
    settings = load_settings()
    checks = []

    def report(name, ok, detail):
        checks.append(ok)
        print(f"{'OK  ' if ok else 'WAIT'} {name}: {detail}")

    if settings.mode == "demo":
        report("模式", True, "模拟模式；设置 FRIDAY_CONFIG=friday.home.json 检查真实链路")
        return 0
    async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
        report("ASR / Vision", False, "共享 ARL 媒体契约待提供；不启动第二套模型服务")
        arl = await runtime_for(settings).status()
        report("共享 ARL", arl["ready"], json.dumps(arl, ensure_ascii=False))
        credentials = Credentials(settings, client)
        if credentials.connected():
            try:
                entities = await HAHome(settings, client, credentials).discover()
                controls = [e for e in entities if e.capabilities]
                report(
                    "Home Assistant",
                    True,
                    f"{len(entities)} 个实体，{len(controls)} 个可控；"
                    + str(sum(e.state == "unavailable" for e in controls))
                    + " 个不可用",
                )
            except Exception as exc:
                report("Home Assistant", False, type(exc).__name__)
        else:
            report("Home Assistant", False, "请在 Friday 页面登录授权")
        token = os.environ.get(settings.miloco_token_env)
        config = Path("data/miloco/config.json")
        if not token and config.exists():
            token = json.loads(config.read_text()).get("server", {}).get("token")
        try:
            r = await client.get(
                settings.miloco_url.rstrip("/") + "/api/miot/status",
                headers={"Authorization": "Bearer " + (token or "")},
            )
            r.raise_for_status()
            value = r.json()
            report("Miloco 服务", value.get("code") == 0, "本机服务可访问")
            report(
                "米家账号",
                bool((value.get("data") or {}).get("is_bound")),
                "已绑定" if (value.get("data") or {}).get("is_bound") else "请在 Miloco 完成账号授权",
            )
        except Exception as exc:
            report("Miloco", False, type(exc).__name__)
        report(
            "摄像头配置",
            bool(settings.camera_did),
            "已配置；尚须验收音视频取流" if settings.camera_did else "未指定米家摄像头",
        )
        print("INFO 此检查不代表真实语音、视觉识别或物理设备动作通过。")
    return 0 if all(checks) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
