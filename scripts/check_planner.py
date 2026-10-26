"""Real planner smoke test with synthetic devices. Never connects to HA."""

import asyncio
import json
import time
from pathlib import Path

from friday.config import Settings
from friday.home import Entity, capabilities, validate_action
from friday.orchestrator import Plan
from friday.providers import ARLPlanner


async def main():
    services = {
        "light": {"turn_on": {}, "turn_off": {}},
        "climate": {"set_hvac_mode": {}, "set_temperature": {}},
    }
    devices = [
        Entity(
            id="light.living",
            name="客厅主灯",
            room="客厅",
            domain="light",
            state="on",
            attributes={"brightness": 255},
            capabilities=capabilities("light", {"supported_color_modes": ["brightness"]}, services),
        ),
        Entity(
            id="light.bedroom",
            name="卧室灯",
            room="卧室",
            domain="light",
            state="on",
            capabilities=capabilities("light", {}, services),
        ),
        Entity(
            id="light.camera_indicator",
            name="摄像头指示灯",
            room="客厅",
            category="config",
            domain="light",
            state="on",
            capabilities=capabilities("light", {}, services),
        ),
        Entity(
            id="climate.living",
            name="客厅空调",
            room="客厅",
            domain="climate",
            state="off",
            attributes={"current_temperature": 29, "hvac_modes": ["off", "cool"], "supported_features": 1},
            capabilities=capabilities(
                "climate",
                {"supported_features": 1, "hvac_modes": ["off", "cool"], "min_temp": 16, "max_temp": 30},
                services,
            ),
        ),
    ]
    provider = ARLPlanner(Settings())
    results = []
    for text in [
        "关闭除卧室以外所有房间的照明灯，其他设备不要动",
        "如果客厅温度高于28度，把客厅空调设为制冷26度，同时把客厅主灯调至30%",
    ]:
        start = time.monotonic()
        plan = Plan.model_validate(
            await provider.plan(
                {"request": text, "camera_room": "客厅", "devices": [d.model_dump() for d in devices]},
                Plan.model_json_schema(),
            )
        )
        for step in plan.steps:
            validate_action(step, {d.id: d for d in devices})
        if text.startswith("关闭"):
            assert [s.entity_id for s in plan.steps] == ["light.living"] and plan.steps[
                0
            ].service == "turn_off"
        else:
            assert any(
                s.entity_id == "climate.living" and s.parameters.get("temperature") == 26 and s.conditions
                for s in plan.steps
            )
            assert any(
                s.entity_id == "light.living" and s.parameters.get("brightness_pct") == 30 for s in plan.steps
            )
        result = {"case": text, "seconds": round(time.monotonic() - start, 2), "plan": plan.model_dump()}
        print(json.dumps(result, ensure_ascii=False), flush=True)
        results.append(result)
    Path("data/planner-verification.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
