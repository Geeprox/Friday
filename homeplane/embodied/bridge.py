"""Worker -> Friday command port; device authority remains in the Console."""

import httpx

from .paths import PRIVATE, camera, configuration


async def command(device, action, event_id, target=None, camera_id=None, gesture=None):
    config = configuration()
    async with httpx.AsyncClient(trust_env=False, timeout=35, follow_redirects=False) as client:
        response = await client.post(
            config["main_origin"] + "/api/embodied/command",
            headers={
                "Origin": config["main_origin"],
                "X-Friday-Worker": (PRIVATE / "control-key").read_text().strip(),
            },
            json=dict(device=device, action=action, event_id=event_id, room=camera(camera_id)["room"], target=target, gesture=gesture),
        )
        if response.status_code != 200:
            return dict(status="unavailable", acknowledged=False, reached_target=False)
        return response.json()
